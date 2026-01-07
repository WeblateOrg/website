PACKAGES: dict[int, int] = {
    10000: 470,
    40000: 700,
    160000: 1140,
    640000: 1930,
    2560000: 3420,
    10240000: 6160,
    40960000: 11220,
    163840000: 20590,
    655360000: 37930,
}
DEDICATED_LIMIT = 160000
DEDICATED_PREFIX = "dedicated:"
HOSTED_PREFIX = "hosted:"
MONTHLY_SUFFIX = "-m"

SUPPORT_PACKAGES: list[tuple[str, str, int]] = [
    ("Weblate backup service (yearly)", "backup", 300),
    ("Weblate basic self-hosted support (yearly)", "basic", 645),
    ("Weblate extended self-hosted support (yearly)", "extended", 1275),
    ("Weblate premium self-hosted support (yearly)", "premium", 2550),
    ("Weblate installation on your Linux server", "install:linux", 480),
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
