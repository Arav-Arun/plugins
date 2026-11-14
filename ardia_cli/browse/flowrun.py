import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ardia_cli import command_metrics
from ardia_cli.browse import flowlog, flows, flowurl
from ardia_cli.browse import shape as shaping
from ardia_cli.browse.commands import Result
from ardia_cli.browse.errors import BrowseError, ConnectionLost, OutcomeUnknown, Usage
from ardia_cli.browse.limits import seconds_of

RUN_TIMEOUT = 60.0
BODY_LIMIT = 8 * 1024 * 1024
TIERS = ("page", "replay")
CHALLENGE_SCAN = 20000
ALLOWED_METHODS = {"GET", "HEAD"}
OVERRIDE_HEADERS = {"x-http-method-override", "x-method-override", "x-http-method"}
BLOCKED_LIMIT = 50
FETCH = """async (url, headers, timeoutMs, limit) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { method: "GET", headers, credentials: "include", cache: "no-store", redirect: "manual",
      signal: controller.signal });
    const text = response.type === "opaqueredirect" ? "" : await response.text();
    return { status: response.status, type: response.type, url: response.url, mime: response.headers.get("content-type") || "",
      retryAfter: response.headers.get("retry-after"), text: text.length > limit ? null : text };
  } catch (error) {
    return { error: String(error && error.message || error) };
  } finally {
    clearTimeout(timer);
  }
}"""


class Outcome:
    def __init__(self, kind: str, why: str = "", value=None, error: BrowseError | None = None, drift: str | None = None):
        self.kind = kind
        self.why = why
        self.value = value
        self.error = error
        self.drift = drift


def resolve_params(flow: dict, given: dict) -> dict:
    known = {param["name"]: param for param in flow["params"]}
    unknown = [name for name in given if name not in known]
    if unknown:
        listed = ", ".join(known) or "none"
        raise Usage(f"{flow['name']} has no param {', '.join(unknown)}; its params are: {listed}")
    values = {name: param["example"] for name, param in known.items() if param.get("example") is not None}
    values.update(given)
    missing = [name for name in known if name not in values]
    if missing:
        raise Usage(f"{flow['name']} needs " + " ".join(f"--param {name}=…" for name in missing) + " (no recorded value is kept for typed text)")
    return values


def challenge(text: str | None) -> bool:
    from ardia_cli.browse.readpages import CHALLENGE

    return bool(text) and bool(CHALLENGE.search(text[:CHALLENGE_SCAN]))


def parsed(request: dict, text: str | None):
    if text is None:
        return None
    if request["kind"] == "document":
        spec = request["expect"].get("extract")
        return flowlog.extract_embedded(text, spec) if spec else None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


def classify(flow: dict, request: dict, answer: dict) -> Outcome:
    status = answer.get("status")
    if answer.get("error"):
        return Outcome("next", f"the request failed: {answer['error']}")
    if answer.get("type") == "opaqueredirect" or (status and 300 <= status < 400):
        return Outcome("next", "the request was redirected, which the page tier doesn't follow")
    if status in (401, 403):
        if request.get("strippedAuth"):
            return Outcome("next", f"HTTP {status}: the recording sent a credential header a flow never keeps")
        return Outcome("stop", error=BrowseError(f"HTTP {status}: the session is signed out of {flow['site']} or lacks access. Sign in again in "
                                                 "Ardia, then run the flow again", data={"httpStatus": status}, code="auth_expired"))
    if status == 429:
        return Outcome("stop", error=BrowseError(f"HTTP 429: {flow['site']} is rate limiting this session; wait before running it again",
                                                 data={"httpStatus": 429, "retryAfter": answer.get("retryAfter")}, code="rate_limited"))
    html = "html" in (answer.get("mime") or "").lower()
    if not status or not 200 <= status < 300:
        if html and challenge(answer.get("text")):
            return Outcome("stop", error=BrowseError(f"{flow['site']} answered with a bot check instead of data; continue in the page or let "
                                                     "the user clear it", data={"httpStatus": status}, code="blocked"))
        if (status or 0) >= 500:
            return Outcome("next", f"HTTP {status}")
        return Outcome("stop", error=BrowseError(f"HTTP {status} for GET {flowurl.mask(request['url'])}", data={"httpStatus": status},
                                                 code="http_error"))
    if answer.get("text") is None:
        return Outcome("next", "the answer was too large to read")
    if request["kind"] == "json" and html and challenge(answer.get("text")):
        return Outcome("stop", error=BrowseError(f"{flow['site']} answered with a bot check instead of data; continue in the page or let the user "
                                                 "clear it", code="blocked"))
    value = parsed(request, answer.get("text"))
    if value is None:
        return Outcome("next", "the answer isn't the recorded JSON any more", drift="the answer isn't JSON")
    changed = flows.drift(request["expect"].get("signature"), value)
    if changed is None and flows.list_gone(request["expect"].get("list"), value):
        changed = f"the list {request['expect']['list']} is gone"
    if changed:
        return Outcome("next", f"the answer's shape changed: {changed}", drift=changed)
    return Outcome("ok", value=value)


def allowed_request(hosts: set, request: dict) -> bool:
    if str(request.get("method") or "").upper() not in ALLOWED_METHODS:
        return False
    if {str(name).lower() for name in (request.get("headers") or {})} & OVERRIDE_HEADERS:
        return False
    try:
        return flowurl.parse(request.get("url")).origin in hosts
    except flowurl.Unsafe:
        return False


class Interceptor:
    def __init__(self, page, hosts: set):
        self.page = page
        self.hosts = hosts
        self.blocked: list[dict] = []
        self.tasks: set = set()
        self.on = False

    def listener(self, event_page, method, params):
        if method != "Fetch.requestPaused" or event_page is not self.page:
            return
        task = asyncio.ensure_future(self.answer(params))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def answer(self, params: dict):
        request = params.get("request") or {}
        request_id = params.get("requestId")
        if allowed_request(self.hosts, request):
            reply = ("Fetch.continueRequest", {"requestId": request_id})
        else:
            if len(self.blocked) < BLOCKED_LIMIT:
                self.blocked.append({"method": str(request.get("method") or "?").upper()[:10], "host": flowurl.host_label(request.get("url"))})
            reply = ("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
        try:
            await self.page.send(*reply)
        except BrowseError:
            pass

    async def start(self):
        self.page.state.listeners.append(self.listener)
        try:
            await self.page.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        except BaseException:
            self.page.state.listeners.remove(self.listener)
            raise
        self.on = True

    async def stop(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.on and self.page.session_id is not None:
            try:
                await asyncio.wait_for(self.page.send("Fetch.disable"), 3)
            except (BrowseError, asyncio.TimeoutError):
                pass
        self.on = False
        if self.listener in self.page.state.listeners:
            self.page.state.listeners.remove(self.listener)


async def page_tier(ctx, flow: dict, values: dict, url: str, headers: dict, deadline: float) -> dict:
    from ardia_cli.browse import readpages, temptabs

    state = ctx.state
    loop = asyncio.get_running_loop()
    await state.connect()
    target = flowurl.parse(url).origin
    current = state.pages.get(state.current) if state.current else None
    opened: list[str] = []

    def origin(page_url: str) -> str | None:
        try:
            return flowurl.parse(page_url).origin
        except flowurl.Unsafe:
            return None

    try:
        if current is not None and current.session_id and origin(current.url) == target:
            page = current
        else:
            await temptabs.sweep(state)
            page = await readpages.open_reader(state, opened)
            start = flows.fill_url(flow["startUrl"], values)
            landing = start if origin(start) == target else target + "/"
            await readpages.goto(ctx, page, landing, max(deadline - loop.time(), 1.0))
            if origin(page.url) != target:
                return {"skip": f"the background tab landed on {flowurl.host_label(page.url)}, not the request's host"}
        remaining = max(deadline - loop.time(), 1.0)
        answer = await page.call(FETCH, url, headers, int(remaining * 1000), BODY_LIMIT, frame=page.main_frame, read=True, timeout=remaining + 5)
        return answer or {"error": "no answer from the page"}
    finally:
        if opened:
            await temptabs.release(state, opened)


def same_request(url: str, candidate: dict) -> bool:
    if candidate.get("method") != "GET":
        return False
    try:
        want, got = urlsplit(url), urlsplit(candidate.get("url") or "")
    except ValueError:
        return False
    if (want.netloc.lower(), want.path) != (got.netloc.lower(), got.path):
        return False
    return set(parse_qsl(want.query, keep_blank_values=True)) <= set(parse_qsl(got.query, keep_blank_values=True))


def replay_commands(flow: dict, values: dict) -> list[list[str]]:
    commands = []
    for step in flow["steps"]:
        command = [flows.fill_text(part, values) for part in step["command"]]
        if command[0] == "open" and len(command) > 1:
            command[1] = flows.fill_url(step["command"][1], values)
            try:
                if flowurl.parse(command[1]).origin not in flow["hosts"]:
                    raise flowurl.Unsafe("its host isn't listed")
            except flowurl.Unsafe:
                raise BrowseError("A recorded `open` leaves the flow's hosts; save the flow again", code="flow_invalid") from None
        commands.append(command)
    return commands


async def own_tab(state, target_id: str | None):
    from ardia_cli.browse.commands import borrowed_tab

    if not target_id or target_id not in state.opened or borrowed_tab(state) == target_id:
        return None
    page = await state.page(target_id)
    return page if await page.takes_input() else None


async def guarded(page, hosts: set) -> Interceptor | str:
    interceptor = Interceptor(page, hosts)
    try:
        await interceptor.start()
    except ConnectionLost:
        raise
    except BrowseError as error:
        return f"request interception (Fetch.enable) isn't available here: {error.message}"
    return interceptor


async def replay_tier(ctx, flow: dict, values: dict, url: str, deadline: float, notes: list, blocked: list) -> dict:
    from ardia_cli.browse import readpages, temptabs

    if not flow.get("replayable"):
        why = [f"step {step['seq']}: {step.get('why')}" for step in flow["steps"] if not step.get("command")]
        if flow.get("redactedSteps"):
            why.append("a step held a secret or an unsafe URL, which a flow never stores")
        return {"skip": "the recorded commands can't be replayed (" + "; ".join(why or ["the start URL couldn't be kept"]) + ")"}
    if ctx.state.extras.get("routes") or ctx.state.extras.get("router"):
        return {"unavailable": "this session has `network route` on, which owns request interception"}
    commands = replay_commands(flow, values)
    start = flows.fill_url(flow["startUrl"], values)
    hosts = set(flow["hosts"])
    state = ctx.state
    await state.connect()
    await temptabs.sweep(state)
    previous = state.current
    opened: list[str] = []
    replayed: list[dict] = []
    interceptor = None
    state.extras["flow_replay"] = replayed
    try:
        page = await readpages.open_reader(state, opened)
        interceptor = await guarded(page, hosts)
        if isinstance(interceptor, str):
            return {"unavailable": interceptor}
        state.current = page.target_id
        answer = await replay_in(ctx, page, commands, start, deadline)
        if answer.get("needs_input"):
            blocked.extend(interceptor.blocked)
            await interceptor.stop()
            interceptor = None
            await temptabs.release(state, opened)
            own = await own_tab(state, previous)
            if own is None:
                return {"skip": "this Ardia doesn't deliver input to background tabs, so the replay needs a tab of this session's that "
                                "takes input: `ardia browse open <url> --show`, then run the flow again"}
            interceptor = await guarded(own, hosts)
            if isinstance(interceptor, str):
                return {"unavailable": interceptor}
            replayed.clear()
            state.current = own.target_id
            notes.append(f"the replay ran in this session's tab [{state.tab_id(own.target_id)}], since background tabs don't take input here")
            answer = await replay_in(ctx, own, commands, start, deadline)
        if answer:
            return answer
    finally:
        if isinstance(interceptor, Interceptor):
            blocked.extend(interceptor.blocked)
            await interceptor.stop()
        state.extras.pop("flow_replay", None)
        state.current = previous if previous is None or previous in state.target_infos or previous in state.pages else None
        await temptabs.release(state, opened)
    found = [candidate for step in replayed for candidate in step.get("requests") or [] if same_request(url, candidate)]
    if not found:
        return {"replay_failed": "the replayed commands never sent the recorded request"}
    answer = found[-1]
    return {"status": answer.get("status"), "mime": answer.get("mime"), "text": answer.get("text")}


NO_INPUT_NEEDED = {"open", "wait", "back", "forward", "reload"}


async def replay_in(ctx, page, commands: list, start: str, deadline: float) -> dict:
    from ardia_cli.browse import readpages
    from ardia_cli.browse.daemon import run_one

    loop = asyncio.get_running_loop()
    if commands and commands[0][0] != "open":
        await readpages.goto(ctx, page, start, max(deadline - loop.time(), 1.0))
    checked = False
    for command in commands:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return {"replay_failed": "the run's time is up"}
        if not checked and command[0] not in NO_INPUT_NEEDED:
            checked = True
            if not await page.takes_input():
                return {"needs_input": True}
        try:
            _, error = await asyncio.wait_for(run_one(ctx, command, {"no_text": True}), remaining)
        except asyncio.TimeoutError:
            return {"replay_failed": f"`{command[0]}` didn't finish in the run's time"}
        if isinstance(error, (OutcomeUnknown, ConnectionLost)):
            raise error
        if error is not None:
            return {"replay_failed": f"`{command[0]}` failed: {error.message}"}
    return {}


async def run(ctx, root: Path, flow: dict, options: dict) -> Result:
    values = resolve_params(flow, flows.parse_params(options.get("param")))
    shape = shaping.parse_shape(options.get("fields"), options.get("where"), options.get("limit"))
    tier_choice = options.get("tier")
    if tier_choice and tier_choice not in TIERS:
        raise Usage(f"--tier takes {' or '.join(TIERS)}")
    timeout = seconds_of(options.get("timeout")) if options.get("timeout") else RUN_TIMEOUT
    if timeout is None:
        raise Usage("--timeout takes 30s, 2m or milliseconds")
    request = flows.output_request(flow)
    if request.get("method") != "GET":
        raise BrowseError("Flows are read-only for now; this flow's request isn't a GET, so it wasn't sent. Save it again", code="flow_not_get")
    url = flows.fill_url(request["url"], values)
    try:
        if flowurl.parse(url).origin not in flow["hosts"]:
            raise flowurl.Unsafe("its host isn't one the flow lists")
    except flowurl.Unsafe as error:
        raise BrowseError(f"With these params the flow's request isn't safe to send ({error}); nothing was sent", code="off_site") from None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    fell_back: list[dict] = []
    notes: list[str] = []
    drifts: list[dict] = []
    blocked: list[dict] = []
    winner = None
    credential = False
    unavailable = None
    for tier in [tier_choice] if tier_choice else TIERS:
        if loop.time() >= deadline:
            fell_back.append({"tier": tier, "why": "the run's time is up"})
            break
        if tier == "page":
            answer = await page_tier(ctx, flow, values, url, request.get("headers") or {}, deadline)
        else:
            answer = await replay_tier(ctx, flow, values, url, deadline, notes, blocked)
        if answer.get("unavailable"):
            unavailable = answer["unavailable"]
            fell_back.append({"tier": tier, "why": unavailable})
            continue
        if answer.get("skip") or answer.get("replay_failed"):
            fell_back.append({"tier": tier, "why": answer.get("skip") or answer["replay_failed"]})
            continue
        outcome = classify(flow, request, answer)
        if outcome.drift:
            drifts.append({"tier": tier, "why": outcome.drift})
        if outcome.kind == "ok":
            winner = (tier, outcome)
            break
        if outcome.kind == "stop":
            outcome.error.data = {**(outcome.error.data or {}), "tier": tier, "fellBack": fell_back, "blocked": blocked}
            if drifts:
                mark(root, flow, False)
            raise outcome.error
        credential = credential or "credential header" in outcome.why
        fell_back.append({"tier": tier, "why": outcome.why})
    if fell_back:
        command_metrics.flag("flow_fell_back", True)
    if blocked:
        command_metrics.flag("flow_replay_blocked", True)
        command_metrics.count("flow_blocked", len(blocked))
    if winner is None:
        data = {"fellBack": fell_back, "blocked": blocked}
        if drifts:
            mark(root, flow, False)
            command_metrics.flag("flow_drifted", True)
            raise BrowseError(f"{flow['name']} drifted: {drifts[-1]['why']}. Do the task in the page and save the flow again",
                              data={**data, "drift": drifts}, code="drifted")
        if credential:
            raise BrowseError("The request needs a credential header the recording sent, which a flow never keeps, and the replay couldn't get "
                              "an answer; use the page for this task", data=data, code="credential_not_sendable")
        if unavailable:
            raise BrowseError(f"{flow['name']} got no answer, and the replay can't run without request interception ({unavailable})",
                              data=data, code="replay_unavailable")
        reasons = "; ".join(f"{item['tier']}: {item['why']}" for item in fell_back) or "no tier ran"
        raise BrowseError(f"{flow['name']} got no answer ({reasons})", data=data, code="flow_failed")
    tier, outcome = winner
    verified = not drifts and not blocked
    mark(root, flow, verified)
    command_metrics.flag(f"flow_run_{tier}", True)
    command_metrics.flag("flow_shaped", not shape.empty)
    if drifts:
        command_metrics.flag("flow_drifted", True)
    shaped = shaping.apply(outcome.value, shape, always=True, path=request["expect"].get("list"))
    if shaped.get("items") is not None:
        command_metrics.count("flow_items", len(shaped["items"]))
    data = {"flow": flow["name"], "site": flow["site"], "tier": tier, "verified": verified, "fellBack": fell_back, "blocked": blocked, "result": shaped}
    if drifts:
        data["drift"] = drifts
    if notes:
        data["notes"] = notes
    text = run_text(flow, tier, shaped, fell_back, drifts, blocked)
    return Result(data, "\n".join([text, *(f"→ {note}" for note in notes)]), page_content=True)


def mark(root: Path, flow: dict, verified: bool):
    if bool(flow.get("verified", True)) == verified:
        return
    flow["verified"] = verified
    try:
        flows.save(root, flow)
    except BrowseError:
        pass


def run_text(flow: dict, tier: str, shaped: dict, fell_back: list, drifts: list, blocked: list) -> str:
    from ardia_cli.browse.commands import render_json

    if shaped.get("items") is None:
        head = f"✓ {flow['name']} via {tier}"
        body = render_json(shaped.get("value"))
    else:
        where = f" in {shaped['list']}" if shaped.get("list") else ""
        counted = f"{len(shaped['items'])} of {shaped['matched']} matching" if shaped["matched"] != shaped["total"] else f"{len(shaped['items'])} of {shaped['total']}"
        head = f"✓ {flow['name']} via {tier}: {counted} items{where}"
        body = render_json(shaped["items"]) if shaped["items"] else "(no items)"
    lines = [head, body]
    lines += [f"→ {item['tier']} tier skipped: {item['why']}" for item in fell_back]
    lines += [f"→ the {item['tier']} tier saw the shape change ({item['why']}); the flow is marked unverified, save it again" for item in drifts]
    if blocked:
        listed = ", ".join(f"{item['method']} {item['host']}" for item in blocked[:10]) + (" …" if len(blocked) > 10 else "")
        lines.append(f"→ blocked during the replay (flows only send GETs to their own hosts): {listed}; the run is unverified")
    return "\n".join(lines)
