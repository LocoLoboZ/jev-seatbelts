#!/bin/sh

script=$1
shift

is_python3() {
    "$@" -c 'import sys; raise SystemExit(sys.version_info.major != 3)' \
        >/dev/null 2>&1
}

platform=$(uname -s 2>/dev/null || :)
case "$platform" in
    MINGW*|MSYS*|CYGWIN*)
        if is_python3 py -3; then
            exec py -3 "$script" "$@"
        elif is_python3 python; then
            exec python "$script" "$@"
        elif is_python3 python3; then
            exec python3 "$script" "$@"
        fi
        ;;
    *)
        if is_python3 python3; then
            exec python3 "$script" "$@"
        elif is_python3 python; then
            exec python "$script" "$@"
        elif is_python3 py -3; then
            exec py -3 "$script" "$@"
        fi
        ;;
esac

echo "jev-seatbelts: Python 3 interpreter not found" >&2
exit 127
