# TTS Pronunciation Overrides

**Status:** Implemented (session 10, 2026-08-13)
**Applies to:** `src/chao/outputs/tts.py`

## Problem

Piper does not decide pronunciation. Text is phonemized by **espeak-ng** before
the model ever sees it; the trained model only supplies timbre. This means
mispronunciations cannot be fixed by more training — they have to be fixed in
the text handed to Piper.

Concrete case: "chao" should be pronounced *chow*. espeak's English
letter-to-sound rules see `-ao` and produce roughly "cha-oh."

## Approach

A substitution table applied at the TTS boundary — after the director strips
inline tags (§9), immediately before synthesis.

```python
import re

_PRONUNCIATION = {
    "chao": "chow",
    "chao's": "chow's",
}

# Letters only, with an apostrophe allowed *between* letters (contractions/
# possessives like "chao's") -- not at the edges, so a quoted 'chao' doesn't
# sweep the closing quote into the match and silently miss the dict lookup.
_WORD_PATTERN = re.compile(r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b")


def _fix_pronunciation(text: str) -> str:
    """Rewrite words espeak-ng mispronounces. TTS path only."""
    def replace(m):
        word = m.group(0)
        fixed = _PRONUNCIATION.get(word.lower())
        if fixed is None:
            return word
        return fixed.capitalize() if word[0].isupper() else fixed

    return _WORD_PATTERN.sub(replace, text)
```

(The original `\b[\w']+\b` draft above had a real bug: a quoted word like
`'chao'` would sweep the closing quote's apostrophe into the match, so the
dict lookup missed silently. Caught during review before implementation;
the pattern actually shipped is the one above.)

## Invariants

- **TTS path only.** The dashboard, `episodes` table, and everything the
  reflection job reads must keep the real spelling. If "chow" leaks into memory,
  retrieved context will teach the LLM to write it that way, and the
  misspelling becomes self-reinforcing.
- Apply *after* tag stripping, so tag syntax is never matched by the regex.
- Table is data, not logic — it belongs in config eventually (`chao.yaml` or a
  sibling `pronunciation.yaml`) so it can be edited without a code change.

## Caveats

- **"chaos" is a real English word.** espeak will say "KAY-oss." If the chao
  ever refers to multiple chao, this fires. The plural for chao (the entities) is chao.
- Capitalization handling above is naive (`.capitalize()`), which will mangle
  all-caps input. Fine for now; revisit if the chao ever shouts.

## Alternative considered: espeak-ng custom dictionary

espeak-ng supports custom pronunciation entries via its `en_list` source file,
recompiled into the dictionary binary. More robust — applies anywhere espeak
runs, and handles phoneme-level control rather than spelling hacks.

Rejected for now: requires maintaining a patched espeak build inside the Piper
install, which is a lot of moving parts for one word. Reconsider if the
substitution table grows unwieldy or if a word resists spelling-based fixes.

## Expected growth

This table will not stay at one entry. Anticipated sources of
mispronunciation:

- Proper nouns from games being streamed
- Viewer usernames spoken aloud (see §15 — usernames are already sanitized
  before speech; pronunciation fixes belong in the same pass)
- Stream slang and emote names