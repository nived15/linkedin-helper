"""Filter validation and URL assembly for LinkedIn search."""

from urllib.parse import parse_qs, urlsplit

import pytest

from linkedin_mcp.scrape.filters import (
    CONNECTION_DEGREES,
    FilterError,
    PeopleSearchFilters,
    PostSearchFilters,
    normalise_facet_id,
)
from linkedin_mcp.scrape.urls import (
    PEOPLE_SEARCH_URL,
    POST_SEARCH_URL,
    group_id_from,
    group_members_url,
    people_search_url,
    post_search_url,
)

US_GEO = "103644278"
MICROSOFT = "1035"


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_every_people_filter_maps_onto_its_linkedin_parameter():
    filters = PeopleSearchFilters(
        keywords="platform engineering",
        connection_degrees=("1st", "2nd", "3rd+"),
        geo_urns=(US_GEO,),
        current_companies=(MICROSOFT,),
        past_companies=("2382910",),
        industries=("4",),
        schools=("18043",),
        title="Solution Engineer",
        first_name="Nived",
        last_name="Velayudhan",
        service_categories=("2340",),
        profile_languages=("en", "fr"),
    )

    params = filters.to_params()

    assert params == {
        "keywords": "platform engineering",
        "network": '["F","S","O"]',
        "geoUrn": f'["{US_GEO}"]',
        "currentCompany": f'["{MICROSOFT}"]',
        "pastCompany": '["2382910"]',
        "industry": '["4"]',
        "schoolFilter": '["18043"]',
        "serviceCategory": '["2340"]',
        "profileLanguage": '["en","fr"]',
        "titleFreeText": "Solution Engineer",
        "firstName": "Nived",
        "lastName": "Velayudhan",
    }


def test_connection_degrees_accept_human_labels_and_linkedin_codes():
    labels = PeopleSearchFilters(connection_degrees=tuple(CONNECTION_DEGREES))
    codes = PeopleSearchFilters(connection_degrees=("F", "S", "O"))
    aliases = PeopleSearchFilters(connection_degrees=("first", "2", "third"))

    assert labels.connection_degrees == ("F", "S", "O")
    assert codes.connection_degrees == ("F", "S", "O")
    assert aliases.connection_degrees == ("F", "S", "O")


def test_an_unknown_connection_degree_is_refused_rather_than_sent():
    with pytest.raises(FilterError, match="4th"):
        PeopleSearchFilters(connection_degrees=("4th",))


def test_a_facet_urn_is_reduced_to_the_id_linkedin_expects():
    filters = PeopleSearchFilters(geo_urns=(f"urn:li:fsd_geo:{US_GEO}",))

    assert filters.geo_urns == (US_GEO,)
    assert normalise_facet_id("geo_urns", f"urn:li:fsd_geo:{US_GEO}") == US_GEO


def test_a_facet_id_that_is_not_an_id_is_refused():
    with pytest.raises(FilterError, match="facet id"):
        PeopleSearchFilters(current_companies=("Microsoft Corporation",))


def test_a_facet_list_given_as_a_bare_string_is_refused():
    with pytest.raises(FilterError, match="sequence of ids"):
        PeopleSearchFilters(geo_urns=US_GEO)


def test_duplicate_facet_ids_collapse_but_keep_caller_order():
    filters = PeopleSearchFilters(industries=("96", "4", "96"))

    assert filters.industries == ("96", "4")


def test_a_profile_language_must_be_a_two_letter_code():
    with pytest.raises(FilterError, match="two letter code"):
        PeopleSearchFilters(profile_languages=("english",))


def test_blank_free_text_is_refused_rather_than_silently_dropped():
    with pytest.raises(FilterError, match="keywords"):
        PeopleSearchFilters(keywords="   ")


def test_overlong_free_text_is_refused():
    with pytest.raises(FilterError, match="character limit"):
        PeopleSearchFilters(keywords="x" * 500)


def test_a_search_with_no_filters_at_all_is_refused():
    with pytest.raises(FilterError, match="at least one filter"):
        PeopleSearchFilters()


def test_the_people_url_percent_encodes_the_json_facet_lists():
    url = people_search_url(
        PeopleSearchFilters(keywords="github copilot", geo_urns=(US_GEO,))
    )

    assert url.startswith(PEOPLE_SEARCH_URL + "?")
    assert "%5B%22103644278%22%5D" in url
    assert query_of(url)["geoUrn"] == [f'["{US_GEO}"]']
    assert query_of(url)["keywords"] == ["github copilot"]
    assert query_of(url)["origin"] == ["FACETED_SEARCH"]


def test_the_first_page_carries_no_page_parameter():
    filters = PeopleSearchFilters(keywords="copilot")

    assert "page=" not in people_search_url(filters, 1)
    assert query_of(people_search_url(filters, 4))["page"] == ["4"]


def test_a_page_below_one_is_refused():
    with pytest.raises(ValueError, match="pages start at 1"):
        people_search_url(PeopleSearchFilters(keywords="copilot"), 0)


def test_post_filters_map_onto_the_content_search_parameters():
    filters = PostSearchFilters(
        keywords="github copilot",
        date_posted="past-week",
        sort_by="date_posted",
        author_companies=(MICROSOFT,),
    )

    params = filters.to_params()

    assert params["keywords"] == "github copilot"
    assert params["datePosted"] == '["past-week"]'
    assert params["sortBy"] == '"date_posted"'
    assert params["authorCompany"] == f'["{MICROSOFT}"]'
    assert post_search_url(filters, 2).startswith(POST_SEARCH_URL + "?")


def test_an_unknown_date_window_is_refused():
    with pytest.raises(FilterError, match="date_posted"):
        PostSearchFilters(keywords="copilot", date_posted="past-year")


def test_an_unknown_sort_order_is_refused():
    with pytest.raises(FilterError, match="sort_by"):
        PostSearchFilters(keywords="copilot", sort_by="engagement")


def test_a_group_id_is_read_from_an_id_or_a_url():
    assert group_id_from(12345) == "12345"
    assert group_id_from("12345") == "12345"
    assert group_id_from("https://www.linkedin.com/groups/12345/") == "12345"
    assert group_members_url(12345).endswith("/groups/12345/members/")


def test_something_that_is_not_a_group_is_refused():
    with pytest.raises(ValueError, match="group id"):
        group_members_url("https://www.linkedin.com/company/microsoft/")


def test_describe_returns_the_parameters_a_run_row_stores():
    filters = PeopleSearchFilters(keywords="copilot", geo_urns=(US_GEO,))

    assert filters.describe() == filters.to_params()
