# Sourced by apply-deflag.sh and build-variant.sh. Loads fixture.env, then fixture.env.example;
# a variable already in the environment wins.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for f in "$ROOT/fixture.env" "$ROOT/fixture.env.example"; do
  [ -f "$f" ] || continue
  while IFS='=' read -r k v; do
    [[ $k =~ ^[A-Z_][A-Z0-9_]*$ ]] && [ -z "${!k+x}" ] && { v=${v#[\"\']}; export "$k=${v%[\"\']}"; }
  done < "$f"
done
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
TARGET="$FIXTURE_SSH_USER@$FIXTURE_HOST"
D="${FIXTURE_DEMO_DIR#\~/}"  # absolute, or relative to the home directory

# Commands start in the home directory on the fixture: this machine when FIXTURE_SSH_USER is
# empty, else the ssh host. fx_line CMD prints the shell line that runs CMD there; fx runs it.
fx_line() {
  if [ -z "$FIXTURE_SSH_USER" ]; then printf 'cd ~ && %s' "$1"; else printf 'ssh %s %s %q' "$SSH_OPTS" "$TARGET" "$1"; fi
}
fx() { bash -c "$(fx_line "$1")"; }
# cp_line SRC... DEST prints the cp (or scp) line that copies local files to DEST on the fixture.
cp_line() {
  local dest="${!#}"
  if [ -z "$FIXTURE_SSH_USER" ]; then printf 'cd ~ && cp %s %s' "${*:1:$#-1}" "$dest"
  else printf 'scp -q %s %s %s:%s' "$SSH_OPTS" "${*:1:$#-1}" "$TARGET" "$dest"; fi
}
