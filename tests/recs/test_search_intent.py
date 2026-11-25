import datetime as dt

from app.recs.search_intent import interpret

TODAY = dt.date(2026, 11, 17)


def test_browse_explicit_dates_and_media():
    i = interpret('scary movies from the 80s', today=TODAY)
    assert i.kind == 'browse' and i.media == ('movie',)
    assert i.genres == ('horror',)
    assert i.date.required and i.date.start.year == 1980 and i.date.end.year == 1989


def test_remembered_year_is_preferred_and_both_media():
    i = interpret('IT 2026', today=TODAY)
    assert i.kind == 'remembered_title' and i.entities == ('IT',)
    assert i.media == ('movie', 'tv') and not i.date.required
    assert interpret('IT released only in 2026', today=TODAY).date.required


def test_setting_exclusions_and_written_decades():
    assert interpret('horror set in the 80s', today=TODAY).date is None
    i = interpret('obscure eighties horror without vampires', today=TODAY)
    assert i.date.required and i.date.start.year == 1980
    assert i.excluded_topics == ('vampires',) and i.preference == 'obscurity'
    assert not i.topics


def test_explicit_media_overrides_planner_and_bad_json_falls_back():
    i = interpret('animated movies with cats', {'media': 'tv', 'topics': ['cat']}, TODAY)
    assert i.media == ('movie',) and i.genres == ('animation',)
    assert interpret('IT 2026', '{bad', TODAY).entities == ('IT',)


def test_before_after_ranges():
    assert interpret('horror before 1990', today=TODAY).date.end.year == 1989
    assert interpret('horror after 2000', today=TODAY).date.start.year == 2001
    assert interpret('horror between 1980 and 1985', today=TODAY).date.end.year == 1985


def test_relative_year_and_similarity_reference():
    i = interpret('horror movies from last year', today=TODAY)
    assert i.date.required and i.date.start.year == 2025
    i = interpret('movies like The Thing', today=TODAY)
    assert i.kind == 'similar_titles' and i.entities == ('The Thing',)


def test_model_noise_cannot_turn_decade_browse_into_topic_search():
    raw = {'topics': ['80s cinema', 'scary movies', 'classic horror', 'vintage films'],
           'entities': ['80s', 'scary movies'], 'search_queries': ['horror movies 80s']}
    i = interpret('scary movies from the 80s', raw, TODAY)
    assert i.topics == () and i.entities == () and i.queries == ()


def test_model_echo_cannot_displace_original_title():
    raw = {'topics': ['technology', '2026'], 'entities': ['IT 2026'],
           'search_queries': ['IT 2026 movie']}
    i = interpret('IT 2026', raw, TODAY)
    assert i.entities == ('IT',) and i.queries == ()


def test_ambiguous_remembered_title_keeps_both_formats():
    i = interpret('IT 2026', {'media': 'movie', 'topics': ['technology']}, TODAY)
    assert i.media == ('movie', 'tv') and i.topics == ()
    i = interpret('animated movies with cats', {'topics': ['cats']}, TODAY)
    assert i.topics == ('cat',)


def test_model_genres_cannot_turn_exact_title_into_browse():
    i = interpret('The Thing 2011', {'media': 'tv', 'genres': ['horror'],
                                    'topics': ['alien'], 'entities': ['The Thing']}, TODAY)
    assert i.kind == 'remembered_title' and i.genres == () and i.media == ('movie', 'tv')
    assert i.entities[0] == 'The Thing'
    i = interpret('The Aristocats', {'genres': ['animation']}, TODAY)
    assert i.kind == 'title_lookup' and i.genres == ()


def test_remembered_character_phrase_extracts_salient_clue():
    assert interpret('new IT show', today=TODAY).entities == ('IT',)
    assert interpret('that new Pennywise series', today=TODAY).entities == ('Pennywise',)


def test_negated_model_topic_is_not_positive_requirement():
    i = interpret('80s horror without vampires', {'topics': ['no-vampires']}, TODAY)
    assert i.topics == () and i.excluded_topics == ('vampires',)


def test_written_decade_is_not_a_topic():
    i = interpret('eighties horror classics', {'topics': ['eighties horror', 'classic horror']}, TODAY)
    assert i.topics == () and i.date.required and i.date.start.year == 1980


def test_descriptive_catdog_uses_compound_query_before_raw_phrase():
    i = interpret('that show with a dog and a cat', today=TODAY)
    assert i.entities == () and i.queries == ('CatDog',)


def test_family_in_a_title_does_not_start_genre_browse():
    title = interpret("Family Feud", today=TODAY)
    assert title.kind == "title_lookup" and title.entities == ("Family Feud",)
    assert interpret("family movies", today=TODAY).genres == ("family",)
