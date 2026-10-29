#!/usr/bin/env bash
# Build per-platform AVL GeoViz extension zips with their Python dependencies
# (requirements.in) bundled as wheels.
#   scripts/build_extension.sh <blender> <out_dir>
# PLATFORMS="linux-x64 ..." limits the platforms. Requirements given as
# "name @ git+URL" are built into wheels first, since they aren't on PyPI.
set -euo pipefail
BLENDER=$1; mkdir -p "$2"; OUT=$(realpath "$2"); ROOT=$(realpath "$(dirname "$0")/..")
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
cp -r "$ROOT/src/avl_geoviz" "$STAGE/" && mkdir "$STAGE/avl_geoviz/wheels" "$STAGE/local"
find "$STAGE/avl_geoviz" -name __pycache__ -prune -exec rm -rf {} +

# Requirements without comments; git ones are built locally and listed by name.
REQS=$STAGE/requirements.in
grep -v '^\s*\(#\|$\)' "$ROOT/requirements.in" | while IFS= read -r req; do
  if [[ $req == *" @ git+"* ]]; then
    url=${req#* @ git+}; src=$STAGE/src/${req%% @ *}
    git clone -q --depth 1 "$url" "$src"
    uv build -q --wheel --out-dir "$STAGE/local" "$src"
    echo "${req%% @ *}"
  else
    echo "$req"
  fi
done > "$REQS"

# Blender platform -> uv platform, then pip's wheel tags for it.
declare -A TAGS=([linux-x64]="x86_64-manylinux_2_28 manylinux_2_28_x86_64 manylinux_2_17_x86_64 manylinux2014_x86_64 linux_x86_64"
                 [windows-x64]="x86_64-pc-windows-msvc win_amd64"
                 [macos-arm64]="aarch64-apple-darwin macosx_15_0_arm64 macosx_14_0_arm64 macosx_13_0_arm64 macosx_12_0_arm64 macosx_11_0_arm64 macosx_10_9_universal2")
# rasterio's macOS wheels need macOS 15, so the macOS zip does too.
export MACOSX_DEPLOYMENT_TARGET=15.0
PLATS=(${PLATFORMS:-${!TAGS[@]}})
for bp in "${PLATS[@]}"; do
  read -r uvplat tags <<< "${TAGS[$bp]}"
  # numpy comes from Blender.
  uv pip compile -q --python-version 3.13 --python-platform "$uvplat" --only-binary :all: --no-emit-package numpy \
      --find-links "$STAGE/local" --no-header --no-annotate "$REQS" -o "$STAGE/$bp.txt"
  uv run -q --no-project --python 3.13 --with pip -- pip download -q --no-deps --only-binary :all: --python-version 3.13 \
      $(printf -- '--platform %s ' $tags) --find-links "$STAGE/local" -r "$STAGE/$bp.txt" -d "$STAGE/avl_geoviz/wheels"
done
# Prepended: top-level TOML keys can't follow the manifest's tables.
M=$STAGE/avl_geoviz/blender_manifest.toml
{ printf 'platforms = [%s]\n' "$(printf '"%s", ' "${PLATS[@]}")"
  printf 'wheels = [\n%s]\n' "$(cd "$STAGE/avl_geoviz" && ls wheels/*.whl | sed 's|.*|  "./&",|')"
  cat "$M"; } > "$M.new" && mv "$M.new" "$M"
"$BLENDER" --command extension validate "$STAGE/avl_geoviz"
"$BLENDER" --command extension build --split-platforms --source-dir "$STAGE/avl_geoviz" --output-dir "$OUT"
