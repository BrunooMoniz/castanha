#!/usr/bin/env bash
# Instala somente o worker ASR pinado, com cópia anterior e rollback de health.
set -euo pipefail
umask 077
candidate=${1:?candidato}
sha=${2:?sha256}
destination=${3:-/root/castanha-transcribe-v2.py}
python=${4:-/root/.castanha-env/bin/python}
[[ $destination = /* && $sha =~ ^[a-f0-9]{64}$ && -f $candidate && ! -L $candidate ]]
[[ ! -L $destination && ( ! -e $destination || -f $destination ) ]]
[[ $(sha256sum "$candidate" | cut -d' ' -f1) = "$sha" ]]
exec 9>"${destination}.deploy.lock"
flock -n 9
expected=$("$python" "$candidate" --describe-contract)
[[ -n $expected ]]
staged=$(mktemp "${destination}.candidate.XXXXXX")
install -m 755 "$candidate" "$staged"
previous=""
if [[ -e $destination ]]; then
  previous=$(mktemp "${destination}.previous.XXXXXX")
  cp -p "$destination" "$previous"
fi
sync -f "$staged"
mv -T "$staged" "$destination"
if actual=$("$python" "$destination" --describe-contract) && [[ $actual = "$expected" ]]; then
  sync -f "$destination"
  printf 'worker_installed sha256=%s\n' "$sha"
else
  failed=$(mktemp "${destination}.failed.XXXXXX")
  mv -T "$destination" "$failed"
  if [[ -n $previous ]]; then cp -p "$previous" "$destination"; fi
  printf 'worker_health_failed; previous version restored when present\n' >&2
  exit 1
fi
