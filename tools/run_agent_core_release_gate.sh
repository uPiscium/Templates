#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
    printf '%s\n' 'ERROR: release-gate launcher requires a tool and command' >&2
    exit 2
fi

tool=$1
shift
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT \
    DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH \
    DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH \
    DYLD_VERSIONED_LIBRARY_PATH DYLD_VERSIONED_FRAMEWORK_PATH \
    DYLD_ROOT_PATH DYLD_IMAGE_SUFFIX || {
        printf '%s\n' 'ERROR: cannot sanitize dynamic-loader environment' >&2
        exit 2
    }

selected_python=$(command -v python3) || {
    printf '%s\n' 'ERROR: trusted Python interpreter is unavailable' >&2
    exit 2
}
case "$selected_python" in
    /nix/store/*/bin/python3|/usr/bin/python3|/run/current-system/sw/bin/python3|/nix/var/nix/profiles/default/bin/python3) ;;
    *)
        printf '%s\n' 'ERROR: selected Python interpreter path is not trusted' >&2
        exit 2
        ;;
esac
resolved_python=$(
    "$selected_python" -I -c 'import os, sys; sys.stdout.write(os.path.realpath(sys.executable))'
) || {
    printf '%s\n' 'ERROR: trusted Python interpreter resolution failed' >&2
    exit 2
}
case "$resolved_python" in
    /nix/store/*/bin/python3|/nix/store/*/bin/python3.[0-9]*|/usr/bin/python3|/usr/bin/python3.[0-9]*) ;;
    *)
        printf '%s\n' 'ERROR: resolved Python interpreter target is not trusted' >&2
        exit 2
        ;;
esac
if [ ! -f "$resolved_python" ] || [ ! -x "$resolved_python" ] || [ -L "$resolved_python" ]; then
    printf '%s\n' 'ERROR: resolved Python interpreter target is unavailable or unsafe' >&2
    exit 2
fi

selected_stat=$(command -v stat) || {
    printf '%s\n' 'ERROR: trusted stat executable is unavailable' >&2
    exit 2
}
case "$selected_stat" in
    /nix/store/*/bin/stat|/usr/bin/stat|/run/current-system/sw/bin/stat|/nix/var/nix/profiles/default/bin/stat) ;;
    *)
        printf '%s\n' 'ERROR: selected stat executable path is not trusted' >&2
        exit 2
        ;;
esac
python_owner=$($selected_stat -Lc '%u' "$resolved_python") || {
    printf '%s\n' 'ERROR: cannot inspect trusted Python interpreter ownership' >&2
    exit 2
}
python_mode=$($selected_stat -Lc '%a' "$resolved_python") || {
    printf '%s\n' 'ERROR: cannot inspect trusted Python interpreter permissions' >&2
    exit 2
}
case "$python_mode" in
    [0-7][0-7][0-7]) ;;
    *)
        printf '%s\n' 'ERROR: trusted Python interpreter permissions are invalid' >&2
        exit 2
        ;;
esac
group_mode=${python_mode#?}
group_mode=${group_mode%?}
other_mode=${python_mode#??}
case "$python_owner:$group_mode:$other_mode" in
    0:[0145]:[0145]) ;;
    *)
        printf '%s\n' 'ERROR: trusted Python interpreter is not root-owned and immutable' >&2
        exit 2
        ;;
esac

exec "$resolved_python" -I "$tool" "$@"
