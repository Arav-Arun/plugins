#!/bin/sh
# Usage: plugins/cli/build.sh <out dir>
#   Builds the Command Line plugin folder into <out dir>/cli: the ardia command as scripts/build.sh packs it (the Rust
#   ardia, the bundled CPython with ardia_cli) plus plugin.json with its version set. The version is
#   $ARDIA_PLUGIN_VERSION, else plugin.json's. Ardia refuses links in a plugin's programs and makes .leases itself, so
#   here Python's bin/python3 is the interpreter, not a link, and there's no .leases. Also leaves in <out dir> the
#   command's tarball (ardia-cli-<version>-<platform>.tar.gz) and the folder packed as Ardia's release fetches it
#   (cli-plugin-<version>-<platform>.tar.gz), each with its .sha256.
set -eu

case "${1:-}" in ''|-h|--help) sed -n '2,8s/^# \{0,1\}//p' "$0"; exit 0 ;; esac

here=$(cd "$(dirname "$0")" && pwd)
out=$(mkdir -p "$1" && cd "$1" && pwd)
version=${ARDIA_PLUGIN_VERSION:-$(sed -n 's/^ *"version": *"\([^"]*\)".*/\1/p' "$here/plugin.json" | head -n 1)}

"$here/scripts/build.sh" "$version" "$out"
case $(uname -s) in Darwin) platform=macos-arm64 ;; *) platform=linux-x64 ;; esac
tarball="$out/ardia-cli-$version-$platform.tar.gz"

work=$(mktemp -d "${TMPDIR:-/tmp}/ardia-cli-plugin.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar -xzf "$tarball" -C "$work"
dest="$out/cli"
rm -rf "$dest"
mv "$work/ardia-cli-$version" "$dest"
rmdir "$dest/.leases"
rm "$dest/python/bin/python" "$dest/python/bin/python3"
mv "$dest/python/bin/python3.13" "$dest/python/bin/python3"
links=$(find "$dest" -type l | head -n 5)
[ -z "$links" ] || { echo "build.sh: Ardia copies a plugin's programs without links, and these are links: $links" >&2; exit 1; }
sed "s/^\( *\"version\": *\)\"[^\"]*\"/\1\"$version\"/" "$here/plugin.json" | grep -v '"\$schema"' >"$dest/plugin.json"
[ "$("$dest/bin/ardia" --version)" = "ardia $version" ] || { echo "build.sh: the built ardia doesn't say $version" >&2; exit 1; }
packed="cli-plugin-$version-$platform.tar.gz"
case $(uname -s) in
  Darwin) (cd "$out" && COPYFILE_DISABLE=1 tar --no-mac-metadata -czf "$packed" cli) ;;
  *) (cd "$out" && tar --sort=name --owner=0 --group=0 --numeric-owner -czf "$packed" cli) ;;
esac
(cd "$out" && { command -v sha256sum >/dev/null && sha256sum "$packed" || shasum -a 256 "$packed"; } >"$packed.sha256")
echo "$dest"
