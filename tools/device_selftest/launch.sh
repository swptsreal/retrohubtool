#!/bin/sh
# RetroHub diagnostic app: runs rh_test.py from the SD card and writes the
# report next to it, because this handheld has no console to read.
SDCARD_PATH="${SDCARD_PATH:-/mnt/SDCARD}"
APP="$SDCARD_PATH/Apps/RetroHub"
OUT="$SDCARD_PATH/rh_test_report.txt"

PY=""
for c in "$APP/python/bin/python3" "$SDCARD_PATH/System/bin/python3" \
         "$SDCARD_PATH/.retrohub/python/bin/python3" /usr/bin/python3; do
    if [ -x "$c" ]; then PY="$c"; break; fi
done

{
    echo "=== RH device test $(date 2>/dev/null) ==="
    echo "python: ${PY:-none}"
} > "$OUT"

if [ -z "$PY" ]; then
    echo "no python interpreter found" >> "$OUT"
    exit 1
fi

LD_LIBRARY_PATH="$SDCARD_PATH/System/lib:/usr/trimui/lib:$LD_LIBRARY_PATH" \
PYSDL2_DLL_PATH="$APP/libs:/usr/trimui/lib:/usr/lib64:/usr/lib" \
"$PY" "$SDCARD_PATH/rh_test.py" >> "$OUT" 2>&1

echo "=== done $(date 2>/dev/null) ===" >> "$OUT"
