"""Synchronous Telegram helpers — message splitting and direct HTTP send.

Provides:
  - split_message(): splits long text into Telegram-safe chunks (≤4096 chars),
    preferring newline boundaries.
  - send_telegram_message(): synchronous HTTP send via httpx (used outside
    the async bot context, e.g. from hooks).
"""

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def split_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Tries to split on newlines when possible to preserve formatting.
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""

    for line in text.split("\n"):
        # If single line exceeds max, split it forcefully
        if len(line) > max_length:
            if current_chunk:
                chunks.append(current_chunk.rstrip("\n"))
                current_chunk = ""
            # Split long line into fixed-size pieces
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
        elif len(current_chunk) + len(line) + 1 > max_length:
            # Current chunk is full, start a new one
            chunks.append(current_chunk.rstrip("\n"))
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks
