# References for Chat API + Web UI

## Similar Implementations

### CLI chat REPL

- **Location:** `src/skyrisk/cli.py` (`_print_reply`, `_chat`)
- **Relevance:** the existing front end for the agent.
- **Key patterns:**
  - `_print_reply` sets the layout of each reply: text, then limitations, then a footer with `served_by` and the de-duplicated tools.
  - `_chat` sets the conversation lifecycle: `build_agent` → `Conversation(max_turns=config.max_history_turns)` → `/reset`.

### Agent core

- **Location:** `src/skyrisk/agent/core.py`
- **Relevance:** `Agent.ask`, `AgentReply` and `Conversation` are wrapped as they are.
- **Key patterns:** refused turns stay out of history, which is handled in `Conversation.record`.

### Agent factory

- **Location:** `src/skyrisk/agent/factory.py`
- **Relevance:** `build_agent` wires the real providers. Only the CLI calls it, and it passes the result to `create_app`.

### Test harness

- **Location:** `tests/fakes.py`, `tests/conftest.py`
- **Relevance:**
  - `FakeProvider` scripts model steps and records the histories it receives, which lets tests assert memory.
  - `build_scored_db` builds a synthetic scored database.
