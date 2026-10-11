# /etc/profile.d/sharedbox.sh: environment + login summary for sharedbox friends
if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx "${SHAREDBOX_FRIENDS_GROUP:-friends}"; then
    _sb_conf=/etc/sharedbox/sharedbox.conf
    _sb_jobs=$(sed -n 's/^BUILD_JOBS=//p' "$_sb_conf" 2>/dev/null); _sb_jobs=${_sb_jobs:-2}
    export MAKEFLAGS="-j${_sb_jobs}" CMAKE_BUILD_PARALLEL_LEVEL="${_sb_jobs}" CARGO_BUILD_JOBS="${_sb_jobs}"
    # agents are installed system-wide and updated daily by the owner
    export DISABLE_AUTOUPDATER=1
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
    _sb_android=$(sed -n 's/^ANDROID_SDK_ROOT=//p' "$_sb_conf" 2>/dev/null); _sb_android=${_sb_android:-/opt/android-sdk}
    if [ -d "$_sb_android" ]; then
        export ANDROID_HOME="$_sb_android" ANDROID_SDK_ROOT="$_sb_android"
        export PATH="$PATH:$_sb_android/platform-tools:$_sb_android/cmdline-tools/latest/bin"
    fi
    unset _sb_jobs _sb_conf _sb_android
    case $- in *i*)
        if command -v box >/dev/null; then
            if [ -e "$HOME/.sharedbox/setup-done" ]; then box status 2>/dev/null
            else box welcome 2>/dev/null; fi
        fi ;;
    esac
fi
