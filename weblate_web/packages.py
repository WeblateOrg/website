PACKAGES: dict[int, int] = {
    10000: 450,
    40000: 660,
    160000: 1060,
    640000: 1780,
    2560000: 3130,
    10240000: 5620,
    40960000: 10220,
    163840000: 18740,
    655360000: 34500,
}
DEDICATED_LIMIT = 160000
DEDICATED_PREFIX = "dedicated:"
HOSTED_PREFIX = "hosted:"
MONTHLY_SUFFIX = "-m"

SUPPORT_PACKAGES: list[tuple[str, str, int]] = [
    ("Weblate backup service (yearly)", "backup", 300),
    ("Weblate basic self-hosted support (yearly)", "basic", 600),
    ("Weblate extended self-hosted support (yearly)", "extended", 1200),
    ("Weblate premium self-hosted support (yearly)", "premium", 2400),
]


def package_name(number: int) -> str:
    if number < 1_000_000:
        return f"{number // 1000}k"
    if number < 10_000_000:
        return f"{(number // 100_000) / 10}M"
    if number < 100_000_000:
        return f"{number // 1_000_000}M"
    return f"{(number // 10_000_000) * 10}M"


PACKAGE_NAMES: dict[int, str] = {limit: package_name(limit) for limit in PACKAGES}

PACKAGE_UPGRADES: dict[str, str] = {
    # Used in tests
    "test:test": "test:test",
    "test:test-1": "test:test-2",
    # Migration to new plans
    "dedicated:basic": "dedicated:160k",
    "dedicated:medium": "dedicated:160k",
    "dedicated:advanced": "dedicated:640k",
    "dedicated:enterprise": "dedicated:10m",
}
previous: str | None = None
for name in PACKAGE_NAMES.values():
    if previous is not None:
        PACKAGE_UPGRADES[f"{DEDICATED_PREFIX}{previous}"] = f"{DEDICATED_PREFIX}{name}"
        PACKAGE_UPGRADES[f"{HOSTED_PREFIX}{previous}"] = f"{HOSTED_PREFIX}{name}"
    previous = name
del previous
