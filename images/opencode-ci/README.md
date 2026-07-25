# opencode-ci

`opencode-ci` is a non-root Kungfu CI runtime with pinned OpenCode. It derives
from `kungfu-verify`, adds Node.js 24, pnpm, and OpenCode, and connects only to
an explicitly supplied OpenAI-compatible endpoint. Ollama, model weights,
credentials, host addresses, and provider state stay outside the image.

The v1 contract guarantees OpenCode `1.18.5`, Git, Bash, Python, jq, uv,
Node.js 24, pnpm, the unprivileged `kungfu` user, an empty per-run HOME/XDG
state, JSONL evidence, and an independent verifier as final authority.

## Headless run

```bash
docker run --rm \
  --network ci-model-network \
  -v "$PWD:/work" -w /work \
  -e OPENCODE_BASE_URL=http://ollama:11434/v1 \
  -e OPENCODE_MODEL=qwen-coder-64k \
  -e OPENCODE_CONTEXT=65536 \
  -e OPENCODE_PROMPT='Implement the bounded task described by the fixture.' \
  -e OPENCODE_VERIFY_COMMAND='./ci/verify-agent-output.sh .opencode-ci/events.jsonl' \
  ghcr.io/kungfu-systems/build-images/opencode-ci@sha256:<accepted-digest> \
  opencode-ci-run
```

No OpenCode login or model API key is required for an unauthenticated endpoint.
The runner refuses to start without an independent verifier and writes
`events.jsonl`, `opencode.stderr`, and `run.json`. It requires ordinary network
access only to the supplied endpoint; it does not require privileged mode, a
Docker socket, host credentials, SSH material, or host networking.

`tests/integration.sh` runs real OpenCode against a deterministic compatible
mock and proves read, write, and Bash tool execution. Its negative case returns
Agent success with OpenCode exit zero after a failing Bash tool; the verifier
must still reject it. Real-model qualification is complementary and
non-release-gating.

Consumers roll back by selecting the previous accepted immutable digest. Never
delete or mutate an existing exact tag or digest.
