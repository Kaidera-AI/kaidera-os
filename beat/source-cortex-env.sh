#!/usr/bin/env bash
# Source the project and canonical local Cortex env files for every Beat launcher.
#
# Real secrets live in local-cortex/.env by default. Project-local cortex.env may
# set non-secret runtime knobs, including CORTEX_KEYS_FILE for copied bundles.
# This helper intentionally does not print values; callers may check whether a
# variable is present.

if [ -n "${BASH_SOURCE[0]:-}" ]; then
    _BEAT_ENV_SOURCE="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    # shellcheck disable=SC2296  # zsh's current-file expansion, unreachable in bash.
    _BEAT_ENV_SOURCE="${(%):-%x}"
else
    _BEAT_ENV_SOURCE="$0"
fi

_BEAT_ENV_ROOT="$(cd "$(dirname "${_BEAT_ENV_SOURCE}")/.." && pwd)"

_beat_env_source_file() {
    local _beat_env_file="$1"
    [ -f "${_beat_env_file}" ] || return 0

    while IFS= read -r _beat_env_line || [ -n "${_beat_env_line}" ]; do
        case "${_beat_env_line}" in
            ""|\#*) continue ;;
            export\ *) _beat_env_line="${_beat_env_line#export }" ;;
        esac

        _beat_env_key="${_beat_env_line%%=*}"
        _beat_env_value="${_beat_env_line#*=}"
        [[ "${_beat_env_key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

        case "${_beat_env_value}" in
            \"*\") _beat_env_value="${_beat_env_value#\"}"; _beat_env_value="${_beat_env_value%\"}" ;;
            \'*\') _beat_env_value="${_beat_env_value#\'}"; _beat_env_value="${_beat_env_value%\'}" ;;
        esac
        export "${_beat_env_key}=${_beat_env_value}"
    done <"${_beat_env_file}"
}

_BEAT_ENV_PROJECT_FILE="${_BEAT_ENV_ROOT}/cortex.env"
if [ -f "${_BEAT_ENV_PROJECT_FILE}" ]; then
    _beat_env_source_file "${_BEAT_ENV_PROJECT_FILE}"
fi

_BEAT_ENV_FILE="${CORTEX_KEYS_FILE:-${_BEAT_ENV_ROOT}/local-cortex/.env}"
_beat_env_source_file "${_BEAT_ENV_FILE}"
export CORTEX_KEYS_FILE="${_BEAT_ENV_FILE}"

unset -f _beat_env_source_file
unset _BEAT_ENV_SOURCE
unset _BEAT_ENV_ROOT
unset _BEAT_ENV_PROJECT_FILE
unset _BEAT_ENV_FILE
unset _beat_env_line
unset _beat_env_key
unset _beat_env_value
