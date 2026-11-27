import pytest

from app.services.pricing import PricingError, quote


@pytest.mark.parametrize(
    "service,size,addons,condition,miles,total",
    [
        ("exterior", "sedan", [], "standard", 8.0, 75),
        ("full", "large_suv", ["pet_hair"], "standard", 12.3, 310),
        ("interior", "van", ["stains", "pet_hair"], "standard", 25.0, 285),
        ("interior", "sedan", ["pet_hair"], "worse", 10, 210),
        ("full", "small_suv_truck", [], "bad", 30.2, 312),
        ("interior", "van", [], "terrible", 5, 295),
        ("full", "small_suv_truck", [], "standard", 25.1, 257),
        ("interior", "sedan", [], "standard", 32.4, 181),
    ],
)
def test_worked_examples(services, settings, service, size, addons, condition, miles, total):
    q = quote(service, size, addons, condition, miles, services, settings)
    assert q.total == total
    assert q.requires_approval is False


def test_far_away_requires_approval(services, settings):
    q = quote("full", "van", [], "standard", 61.0, services, settings)
    assert q.requires_approval and q.base == 315 and q.travel_fee == 72


def test_addon_not_applicable_on_exterior(services, settings):
    with pytest.raises(PricingError) as e:
        quote("exterior", "sedan", ["pet_hair"], "standard", 5, services, settings)
    assert e.value.code == "ADDON_NOT_APPLICABLE"


def test_condition_not_applicable_on_exterior(services, settings):
    with pytest.raises(PricingError) as e:
        quote("exterior", "van", [], "bad", 5, services, settings)
    assert e.value.code == "CONDITION_NOT_APPLICABLE"


@pytest.mark.parametrize("miles,fee", [(25.0, 0), (25.01, 2), (32.4, 16), (60.0, 70)])
def test_travel_fee(services, settings, miles, fee):
    assert quote("exterior", "sedan", [], "standard", miles, services, settings).travel_fee == fee


def test_durations(services, settings):
    assert quote("interior", "van", [], "terrible", None, services, settings).duration_min == 270
    assert quote("interior", "sedan", ["pet_hair", "stains"], "standard", None, services, settings).duration_min == 240


def test_inactive_service_rejected(services, settings):
    for r in services:
        if r["service_key"] == "exterior":
            r["active"] = "FALSE"
    with pytest.raises(PricingError):
        quote("exterior", "sedan", [], "standard", None, services, settings)


def test_price_change_reflected(services, settings):
    for r in services:
        if r["service_key"] == "exterior" and r["size_class"] == "sedan":
            r["price"] = 80
    assert quote("exterior", "sedan", [], "standard", None, services, settings).total == 80


def test_amounts_for_guardrail(services, settings):
    q = quote("full", "large_suv", ["pet_hair"], "worse", 30, services, settings)
    assert {285, 25, 20, 10, 340, 330} <= q.amounts()
