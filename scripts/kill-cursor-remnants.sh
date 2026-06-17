#!/usr/bin/env bash
# Ubuntu 上の使われていない Cursor Remote 残骸を一括停止する。
#   - 孤立 cursorsandbox / cursor-server (PPID=1)
#   - closing / abandoned な loginctl セッション内の cursor プロセス
#   - cursor-server 由来の sleep 10 / 孤立 bash
# AppArmor (cursor_sandbox_remote) で kill できない場合はプロファイルを一時解除する。
#
# 使い方:
#   sudo ./scripts/kill-cursor-remnants.sh              # 残骸のみ（全ユーザー）
#   sudo ./scripts/kill-cursor-remnants.sh --dry-run
#   sudo ./scripts/kill-cursor-remnants.sh --user soft1
#   sudo ./scripts/kill-cursor-remnants.sh --all        # 接続中も含め全停止（注意）
set -euo pipefail

DRY_RUN=0
KILL_ALL=0
declare -a TARGET_USERS=()

usage() {
  cat <<'EOF'
Usage: kill-cursor-remnants.sh [OPTIONS]

Options:
  --dry-run       実行せず表示のみ
  --all           接続中の Cursor Remote も含め .cursor-server を全停止
  --user USER     対象ユーザーを限定（複数指定可）
  -h, --help      このヘルプ
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --all) KILL_ALL=1 ;;
    --user)
      shift
      [[ $# -gt 0 ]] || { echo "--user にはユーザー名が必要です" >&2; exit 1; }
      TARGET_USERS+=("$1")
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "不明なオプション: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    echo "+ $*"
    "$@"
  fi
}

cursor_cmd_pattern='\.cursor-server/|cursorsandbox|/multiplex-server/.*cursor|cursor-remote-code\.token'

user_in_targets() {
  local user="$1"
  if [[ ${#TARGET_USERS[@]} -eq 0 ]]; then
    return 0
  fi
  local u
  for u in "${TARGET_USERS[@]}"; do
    [[ "$u" == "$user" ]] && return 0
  done
  return 1
}

is_human_user() {
  local user="$1"
  local uid
  uid="$(id -u "$user" 2>/dev/null)" || return 1
  [[ "$uid" -ge 1000 ]]
}

cursor_users_on_system() {
  ps -eo user=,cmd= 2>/dev/null \
    | grep -E "$cursor_cmd_pattern" \
    | awk '{print $1}' \
    | sort -u
}

resolve_target_users() {
  local user
  if [[ ${#TARGET_USERS[@]} -gt 0 ]]; then
    for user in "${TARGET_USERS[@]}"; do
      id -u "$user" >/dev/null 2>&1 || { echo "ユーザーが存在しません: $user" >&2; exit 1; }
      echo "$user"
    done
    return
  fi
  while read -r user; do
    [[ -n "$user" ]] || continue
    is_human_user "$user" && echo "$user"
  done < <(cursor_users_on_system)
}

session_state() {
  local sid="$1" key val
  for key in State SubState; do
    val="$(loginctl show-session "$sid" -p "$key" --value 2>/dev/null || true)"
    printf '%s:%s ' "$key" "${val:-unknown}"
  done
}

is_stuck_session() {
  local sid="$1" state sub
  state="$(loginctl show-session "$sid" -p State --value 2>/dev/null || true)"
  sub="$(loginctl show-session "$sid" -p SubState --value 2>/dev/null || true)"
  [[ "$state" == "closing" || "$sub" == "abandoned" ]]
}

session_has_cursor() {
  local sid="$1" uid scope pid cmd
  uid="$(loginctl show-session "$sid" -p User --value 2>/dev/null || true)"
  [[ -n "$uid" ]] || return 1
  scope="/sys/fs/cgroup/user.slice/user-${uid}.slice/session-${sid}.scope"
  [[ -f "${scope}/cgroup.procs" ]] || return 1
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    cmd="$(ps -o cmd= -p "$pid" 2>/dev/null || true)"
    [[ "$cmd" =~ $cursor_cmd_pattern ]] && return 0
  done < "${scope}/cgroup.procs"
  return 1
}

scope_has_cursor() {
  local scope="$1" pid cmd
  [[ -f "${scope}/cgroup.procs" ]] || return 1
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    cmd="$(ps -o cmd= -p "$pid" 2>/dev/null || true)"
    [[ "$cmd" =~ $cursor_cmd_pattern ]] && return 0
  done < "${scope}/cgroup.procs"
  return 1
}

pid_in_stuck_session() {
  local pid="$1" sid uid scope
  for sid in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
    is_stuck_session "$sid" || continue
    uid="$(loginctl show-session "$sid" -p User --value 2>/dev/null || true)"
    [[ -n "$uid" ]] || continue
    scope="/sys/fs/cgroup/user.slice/user-${uid}.slice/session-${sid}.scope"
    [[ -f "${scope}/cgroup.procs" ]] || continue
    grep -qx "$pid" "${scope}/cgroup.procs" 2>/dev/null && return 0
  done
  return 1
}

is_cursor_cmd() {
  [[ "$1" =~ $cursor_cmd_pattern ]]
}

is_orphan_cursor_sleep() {
  local ppid="$1" user="$2"
  [[ "$3" == "sleep" ]] || return 1
  local pcmd
  pcmd="$(ps -o cmd= -p "$ppid" 2>/dev/null || true)"
  [[ "$pcmd" =~ $cursor_cmd_pattern || "$pcmd" =~ cursorsandbox ]]
}

is_orphan_cursor_bash() {
  local ppid="$1" cmd="$2"
  [[ "$ppid" == "1" && "$cmd" =~ cursorsandbox ]] && return 0
  [[ "$ppid" == "1" && "$cmd" =~ dump_bash_state ]] && return 0
  return 1
}

declare -a REMNANT_PIDS=()

collect_remnant_pids_for_user() {
  local user="$1"
  local pid ppid comm cmd
  REMNANT_PIDS=()
  while read -r pid ppid comm cmd; do
    [[ -n "$pid" ]] || continue
    if [[ "$KILL_ALL" -eq 1 ]]; then
      if is_cursor_cmd "$cmd" \
        || [[ "$comm" == "sleep" && "$cmd" =~ (sleep 10|sleep 60) ]] \
        || is_orphan_cursor_bash "$ppid" "$cmd"; then
        REMNANT_PIDS+=("$pid")
      fi
      continue
    fi
    if [[ "$ppid" == "1" ]] && { is_cursor_cmd "$cmd" || is_orphan_cursor_bash "$ppid" "$cmd"; }; then
      REMNANT_PIDS+=("$pid")
      continue
    fi
    if is_orphan_cursor_sleep "$ppid" "$user" "$comm"; then
      REMNANT_PIDS+=("$pid")
      continue
    fi
    if is_cursor_cmd "$cmd" && pid_in_stuck_session "$pid"; then
      REMNANT_PIDS+=("$pid")
    fi
  done < <(ps -u "$user" -o pid=,ppid=,comm=,cmd= 2>/dev/null || true)
}

show_cursor_processes() {
  local label="$1"
  shift
  local users=("$@")
  local user
  echo "=== ${label} ==="
  for user in "${users[@]}"; do
    local lines
    lines="$(ps -u "$user" -o pid,ppid,etime,cmd 2>/dev/null | grep -E 'cursor-server|cursorsandbox|sleep 10|multiplex-server' || true)"
    if [[ -n "$lines" ]]; then
      echo "--- ${user} ---"
      echo "$lines"
    fi
  done
  echo "cursor-related (all users): $(ps aux | grep -E '[c]ursor-server|[c]ursorsandbox' | wc -l)"
  echo "sleep 10 (all users): $(ps aux | grep '[s]leep 10' | wc -l)"
}

terminate_stuck_sessions() {
  local user="$1" sid session_user
  while read -r sid session_user _; do
    [[ -n "$sid" ]] || continue
    [[ "$session_user" == "$user" ]] || continue
    if [[ "$KILL_ALL" -eq 1 ]] || is_stuck_session "$sid"; then
      echo "  session ${sid} ($(session_state "$sid"))"
      run loginctl terminate-session "$sid"
    fi
  done < <(loginctl list-sessions --no-legend 2>/dev/null || true)
}

signal_pids() {
  local sig="$1"
  shift
  local pid
  for pid in "$@"; do
    run kill "-${sig}" "$pid" 2>/dev/null || true
  done
}

kill_remnants_for_user() {
  local user="$1"
  collect_remnant_pids_for_user "$user"
  if [[ ${#REMNANT_PIDS[@]} -eq 0 ]]; then
    return 0
  fi
  echo "=== Kill cursor remnants for ${user} (${#REMNANT_PIDS[@]} pids) ==="
  signal_pids TERM "${REMNANT_PIDS[@]}"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    sleep 1
    signal_pids KILL "${REMNANT_PIDS[@]}"
  fi
}

kill_cursor_via_cgroup_for_user() {
  local user="$1" uid scope cg sid
  uid="$(id -u "$user")"
  for scope in /sys/fs/cgroup/user.slice/user-"${uid}".slice/session-*.scope; do
    [[ -d "$scope" ]] || continue
    scope_has_cursor "$scope" || continue
    if [[ "$KILL_ALL" -eq 0 ]]; then
      sid="${scope##*/session-}"
      sid="${sid%.scope}"
      is_stuck_session "$sid" || continue
    fi
    cg="${scope}/cgroup.kill"
    if [[ -f "$cg" ]]; then
      echo "  cgroup.kill $(basename "$scope")"
      run bash -c "echo 1 > '${cg}'"
    fi
    run systemctl stop "$(basename "$scope")" || true
  done
}

kill_all_cursor_for_user() {
  local user="$1" home="/home/${user}"
  echo "=== Kill all .cursor-server for ${user} (--all) ==="
  run pkill -u "$user" -TERM -f "${home}/.cursor-server" 2>/dev/null || true
  run pkill -u "$user" -TERM -f cursorsandbox 2>/dev/null || true
  run pkill -u "$user" -TERM -f '\.cursor-server/' 2>/dev/null || true
  if [[ "$DRY_RUN" -eq 0 ]]; then
    sleep 2
    pkill -u "$user" -KILL -f '\.cursor-server/' 2>/dev/null || true
    pkill -u "$user" -KILL -f cursorsandbox 2>/dev/null || true
  fi
}

kill_cursor_via_apparmor_workaround() {
  local profile=/etc/apparmor.d/cursor-sandbox
  local users=("$@")
  local user
  if [[ ! -f "$profile" ]]; then
    echo "AppArmor プロファイルが見つかりません: ${profile}" >&2
    return 1
  fi
  echo "=== AppArmor が signal を拒否しているためプロファイルを一時解除 ==="
  run apparmor_parser -R "$profile"
  for user in "${users[@]}"; do
    run pkill -u "$user" -KILL -f cursorsandbox || true
    run pkill -u "$user" -KILL -f '\.cursor-server/' || true
  done
  run apparmor_parser -r "$profile"
}

if [[ "$(id -un)" != root ]]; then
  echo "root で実行してください: sudo $0 $*" >&2
  exit 1
fi

mapfile -t USERS < <(resolve_target_users)
if [[ ${#USERS[@]} -eq 0 ]]; then
  echo "対象ユーザーがいません（cursor 残骸なし）"
  exit 0
fi

echo "対象ユーザー: ${USERS[*]}"
if [[ "$KILL_ALL" -eq 1 ]]; then
  echo "モード: --all（接続中の Cursor Remote も停止します）"
else
  echo "モード: 残骸のみ（孤立 PPID=1 / closing・abandoned セッション）"
fi

show_cursor_processes "Before" "${USERS[@]}"

for user in "${USERS[@]}"; do
  echo "=== Terminate stuck sessions for ${user} ==="
  terminate_stuck_sessions "$user"
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  sleep 2
fi

for user in "${USERS[@]}"; do
  if [[ "$KILL_ALL" -eq 1 ]]; then
    kill_all_cursor_for_user "$user"
  else
    kill_remnants_for_user "$user"
  fi
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  sleep 2
fi

needs_force_cleanup() {
  local user
  if [[ "$KILL_ALL" -eq 1 ]]; then
    [[ $(ps aux | grep -cE '[c]ursor-server|[c]ursorsandbox' || true) -gt 0 ]]
    return
  fi
  for user in "${USERS[@]}"; do
    collect_remnant_pids_for_user "$user"
    [[ ${#REMNANT_PIDS[@]} -gt 0 ]] && return 0
  done
  return 1
}

if needs_force_cleanup; then
  echo "=== 残存あり → cgroup / AppArmor 回避 ==="
  for user in "${USERS[@]}"; do
    kill_cursor_via_cgroup_for_user "$user" || true
  done
  if [[ "$DRY_RUN" -eq 0 ]]; then
    sleep 2
  fi
  if [[ "$DRY_RUN" -eq 1 ]] || needs_force_cleanup; then
    kill_cursor_via_apparmor_workaround "${USERS[@]}" || true
  fi
fi

show_cursor_processes "After" "${USERS[@]}"
echo "cursor sessions still open:"
loginctl list-sessions --no-legend 2>/dev/null | while read -r sid uid user _; do
  session_has_cursor "$sid" && echo "  ${sid} ${user} ($(session_state "$sid"))"
done || true
