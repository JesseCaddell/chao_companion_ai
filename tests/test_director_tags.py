from chao.director.tags import ParsedTag, parse_tags, split_sentences, strip_actions, strip_emoji


def test_simple_tag_is_parsed_and_stripped():
    cleaned, tags = parse_tags("[happy] Hi there!")

    assert cleaned == "Hi there!"
    assert tags == [ParsedTag(name="happy", value=None, sentence_index=0)]


def test_trailing_tag_clamps_to_last_sentence_index():
    """A tag with nothing after it (e.g. a sign-off "[happy]") must not
    point past the end of split_sentences(cleaned) — that's an IndexError
    waiting for the director, which schedules against sentence index.
    """
    cleaned, tags = parse_tags("Hi. [happy]")

    assert cleaned == "Hi."
    assert len(split_sentences(cleaned)) == 1
    assert tags == [ParsedTag(name="happy", value=None, sentence_index=0)]


def test_tag_only_input_has_in_range_index_even_with_no_sentences():
    cleaned, tags = parse_tags("[happy]")

    assert cleaned == ""
    assert split_sentences(cleaned) == []
    assert tags == [ParsedTag(name="happy", value=None, sentence_index=0)]


def test_two_tags_two_sentences():
    cleaned, tags = parse_tags("[happy] Oh you're back. [sad] You said five minutes.")

    assert cleaned == "Oh you're back. You said five minutes."
    assert tags == [
        ParsedTag(name="happy", value=None, sentence_index=0),
        ParsedTag(name="sad", value=None, sentence_index=1),
    ]


def test_design_doc_example():
    """Literal §9 example. "Oh!" and "You're back!" each end in a sentence
    terminator, so the simple `.!?`-based splitter counts them as two
    sentences — that's an accepted simplification (no abbreviation/
    interjection handling), not a bug to special-case.
    """
    cleaned, tags = parse_tags("[happy] Oh! You're back! [sad] You said five minutes.")

    assert cleaned == "Oh! You're back! You said five minutes."
    assert tags == [
        ParsedTag(name="happy", value=None, sentence_index=0),
        ParsedTag(name="sad", value=None, sentence_index=2),
    ]


def test_unknown_tag_name_is_stripped_but_not_emitted():
    cleaned, tags = parse_tags("[proud] Nice work.")

    assert cleaned == "Nice work."
    assert tags == []


def test_look_tag_with_known_target():
    cleaned, tags = parse_tags("[look:chat] Hey chat!")

    assert cleaned == "Hey chat!"
    assert tags == [ParsedTag(name="look", value="chat", sentence_index=0)]


def test_look_tag_with_unknown_target_is_dropped():
    cleaned, tags = parse_tags("[look:streamer] Hi")

    assert cleaned == "Hi"
    assert tags == []


def test_fly_tag_with_known_direction():
    cleaned, tags = parse_tags("[fly:left] Watch this!")

    assert cleaned == "Watch this!"
    assert tags == [ParsedTag(name="fly", value="left", sentence_index=0)]


def test_fly_tag_with_unknown_direction_is_dropped():
    cleaned, tags = parse_tags("[fly:up] Whee")

    assert cleaned == "Whee"
    assert tags == []


def test_known_name_with_unexpected_value_is_dropped():
    cleaned, tags = parse_tags("[happy:very] Hi")

    assert cleaned == "Hi"
    assert tags == []


def test_pause_tag_is_recognized():
    _, tags = parse_tags("Hold on [pause] really?")

    assert tags == [ParsedTag(name="pause", value=None, sentence_index=0)]


def test_case_insensitive_tag_name():
    cleaned, tags = parse_tags("[HAPPY] Hi")

    assert cleaned == "Hi"
    assert tags == [ParsedTag(name="happy", value=None, sentence_index=0)]


def test_unclosed_bracket_is_left_as_text_and_does_not_crash():
    text = "[happy Hi there, no closing bracket"

    cleaned, tags = parse_tags(text)

    assert cleaned == text
    assert tags == []


def test_multiword_bracketed_text_is_left_untouched():
    text = "She said [laughs nervously] and left."

    cleaned, tags = parse_tags(text)

    assert cleaned == text
    assert tags == []


def test_text_with_no_tags_is_unchanged_aside_from_whitespace():
    cleaned, tags = parse_tags("Just a normal sentence.")

    assert cleaned == "Just a normal sentence."
    assert tags == []


def test_extra_whitespace_is_collapsed():
    cleaned, _ = parse_tags("[happy]   Hi   there.")

    assert cleaned == "Hi there."


def test_never_crashes_on_garbage_input():
    garbage_inputs = [
        "",
        "[[[[[",
        "]]]]]",
        "[:]",
        "[]",
        "[" * 500,
        "[happy][sad][curious][look:chat][bogus:thing]",
        "\n\n\t[happy]\n\n",
    ]
    for text in garbage_inputs:
        cleaned, tags = parse_tags(text)
        assert isinstance(cleaned, str)
        assert isinstance(tags, list)


def test_split_sentences_basic():
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_split_sentences_empty_string():
    assert split_sentences("") == []


def test_strip_actions_removes_asterisk_wrapped_narration():
    assert strip_actions("*flutters over here* Hi there!") == "Hi there!"


def test_strip_actions_removes_narration_mid_sentence():
    assert strip_actions("Hi there *bounces excitedly* how are you?") == "Hi there how are you?"


def test_strip_actions_all_narration_collapses_to_empty():
    assert strip_actions("*just flies around in a circle*") == ""


def test_strip_actions_multiple_actions_in_one_sentence():
    assert strip_actions("*waves* Hi *sits down* bye") == "Hi bye"


def test_strip_actions_leaves_lone_asterisk_as_literal_text():
    text = "5 * 3 is fifteen"
    assert strip_actions(text) == text


def test_strip_actions_no_asterisks_unchanged_aside_from_whitespace():
    assert strip_actions("Just a normal sentence.") == "Just a normal sentence."


def test_strip_actions_collapses_whitespace_left_behind():
    assert strip_actions("Hi   *pauses*   there") == "Hi there"


def test_strip_emoji_removes_trailing_emoji():
    assert strip_emoji("Hi there! \U0001f60a") == "Hi there!"


def test_strip_emoji_removes_leading_emoji():
    assert strip_emoji("\U0001f389 Congrats!") == "Congrats!"


def test_strip_emoji_removes_emoji_mid_sentence():
    assert strip_emoji("I'm so \U0001f60a happy right now") == "I'm so happy right now"


def test_strip_emoji_all_emoji_collapses_to_empty():
    assert strip_emoji("\U0001f60a") == ""


def test_strip_emoji_removes_zwj_sequence():
    # Family emoji, a ZWJ-joined sequence of four separate codepoints.
    assert strip_emoji("Family time \U0001f468‍\U0001f469‍\U0001f467‍\U0001f466!") == "Family time !"


def test_strip_emoji_removes_flag_emoji():
    assert strip_emoji("\U0001f1fa\U0001f1f8 USA") == "USA"


def test_strip_emoji_no_emoji_unchanged_aside_from_whitespace():
    assert strip_emoji("Just a normal sentence.") == "Just a normal sentence."


def test_strip_emoji_collapses_whitespace_left_behind():
    assert strip_emoji("Hi   \U0001f60a   there") == "Hi there"
