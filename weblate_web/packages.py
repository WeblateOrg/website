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


def package_name(number: int) -> str:
    if number < 1_000_000:
        return f"{number // 1000}k"
    if number < 10_000_000:
        return f"{(number // 100_000) / 10}M"
    if number < 100_000_000:
        return f"{number // 1_000_000}M"
    return f"{(number // 10_000_000) * 10}M"


PACKAGE_NAMES: dict[int, str] = {limit: package_name(limit) for limit in PACKAGES}
