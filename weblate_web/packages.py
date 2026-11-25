PACKAGES: dict[int, int] = {
    10000: 470,
    40000: 700,
    160000: 1140,
    640000: 1930,
    2560000: 3420,
    10240000: 6160,
    20480000: 8300,
    40960000: 11220,
    81920000: 15190,
    163840000: 20590,
    327680000: 27940,
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
    return f"{(number // 10_000_000) * 10}M"


PACKAGE_NAMES: dict[int, str] = {limit: package_name(limit) for limit in PACKAGES}

PACKAGE_UPGRADES: dict[str, tuple[str, ...]] = {
    # Used in tests
    "test:test": ("test:test",),
    "test:test-1": ("test:test-2",),
    # Self-hosted support upgrades
    "basic": ("extended", "premium"),
    "extended": ("premium",),
    # Migration to new plans
    "dedicated:basic": ("dedicated:160k",),
    "dedicated:medium": ("dedicated:160k",),
    "dedicated:advanced": ("dedicated:640k",),
    "dedicated:enterprise": ("dedicated:10m",),
}
previous: str | None = None
for tier_name in PACKAGE_NAMES.values():
    if previous is not None:
        PACKAGE_UPGRADES[f"{DEDICATED_PREFIX}{previous}"] = (
            f"{DEDICATED_PREFIX}{tier_name}",
        )
        PACKAGE_UPGRADES[f"{HOSTED_PREFIX}{previous}"] = (
            f"{HOSTED_PREFIX}{tier_name}",
        )
        PACKAGE_UPGRADES[f"{HOSTED_PREFIX}{previous}{MONTHLY_SUFFIX}"] = (
            f"{HOSTED_PREFIX}{tier_name}{MONTHLY_SUFFIX}",
        )
    previous = tier_name
del previous


# Label, monthly price, source strings, languages, projects.
LEGACY_HOSTED_PACKAGES = {
    "basic": ("Basic", 26, 1000, 15, 1),
    "medium": ("Medium", 68, 3000, 30, 2),
    "advanced": ("Advanced", 136, 10000, 60, 0),
    "advanced-20000": ("Advanced, 20k source strings", 136, 20000, 30, 0),
    "enterprise": ("Enterprise", 411, 50000, 100, 0),
}
HOSTED_PLAN_PACKAGES = {
    "10k": PACKAGE_NAMES[10000].lower(),
    "40k": PACKAGE_NAMES[40000].lower(),
    "160k": PACKAGE_NAMES[160000].lower(),
    "640k": PACKAGE_NAMES[640000].lower(),
    "2-5m": PACKAGE_NAMES[2560000].lower(),
    "10m": PACKAGE_NAMES[10240000].lower(),
    **{slug: slug for slug in LEGACY_HOSTED_PACKAGES},
}
HOSTED_NONPROFIT_PLANS = {
    "10k-3c1a385f": "10k",
    "160k-strings-for-non-profit": "160k",
    "640k-strings-for-non-profit": "640k",
    "basic-for-non-profit": "basic",
    "medium-for-non-profit": "medium",
    "advanced-for-non-profit": "advanced",
    "advanced-for-non-profit-20000": "advanced-20000",
    "enterprise-for-non-profit": "enterprise",
}
HOSTED_PERIOD_SUFFIXES = {"m": MONTHLY_SUFFIX, "y": ""}
PACKAGE_HOSTED_PLANS = {
    f"{HOSTED_PREFIX}{name}{suffix}": slug
    for slug, name in HOSTED_PLAN_PACKAGES.items()
    for suffix in HOSTED_PERIOD_SUFFIXES.values()
}
# Initial successors are chosen by price. Usage-aware suggestions are separate.
for legacy, successor in (
    ("basic", "10k"),
    ("medium", "40k"),
    ("advanced", "640k"),
    ("advanced-20000", "640k"),
    ("enterprise", "10m"),
):
    for suffix in HOSTED_PERIOD_SUFFIXES.values():
        PACKAGE_UPGRADES[f"{HOSTED_PREFIX}{legacy}{suffix}"] = (
            f"{HOSTED_PREFIX}{successor}{suffix}",
        )


def hosted_plan_to_package(slug: str, period: str) -> str:
    """Resolve a Hosted slug and period to a website package name."""
    if period not in HOSTED_PERIOD_SUFFIXES:
        msg = f"Unsupported Hosted billing period: {period!r}. Expected 'm' or 'y'."
        raise ValueError(msg)
    if slug in {"libre", "libre-2-5m", "weblate-unlimited"}:
        msg = f"Noncommercial Hosted plan {slug!r} has no paid package."
        raise ValueError(msg)
    if slug in {"enterprise-collabora", "enterprise-for-non-profit-200-languages"}:
        msg = f"Retired Hosted plan {slug!r} requires explicit migration handling."
        raise ValueError(msg)
    canonical = HOSTED_NONPROFIT_PLANS.get(slug, slug)
    if canonical not in HOSTED_PLAN_PACKAGES:
        msg = f"Unsupported Hosted plan: {slug!r}."
        raise ValueError(msg)
    name = HOSTED_PLAN_PACKAGES[canonical]
    return f"{HOSTED_PREFIX}{name}{HOSTED_PERIOD_SUFFIXES[period]}"


def package_to_hosted_plan(name: str) -> str:
    """Return the canonical Hosted slug, independently of customer discounts."""
    if name not in PACKAGE_HOSTED_PLANS:
        msg = f"Website package {name!r} has no supported Hosted plan."
        raise ValueError(msg)
    return PACKAGE_HOSTED_PLANS[name]


def hosted_plan_requires_nonprofit_discount(slug: str) -> bool:
    """Identify supported plans that require the customer-wide discount."""
    hosted_plan_to_package(slug, "y")
    return slug in HOSTED_NONPROFIT_PLANS
