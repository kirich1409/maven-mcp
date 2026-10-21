"""Fixed version catalogs for candidate selection. No network."""

ONE_PATCH = {
    "pin": "4.12.0",
    "versions": ["4.12.0", "4.12.1"],
    "named": "4.12.1",
}

WIDE = {
    "pin": "1.3.0",
    "versions": ["1.3.6", "1.3.7-rc.1", "1.5.6", "2.0.0", "2.0.0-alpha.1"],
    "named": None,
}

ALPHA_AND_STABLE = {
    "pin": "1.3.0-alpha.2",
    "versions": ["1.3.0-alpha.5", "1.3.6"],
    "named": "1.3.6",
}

ONLY_NEXT_ALPHA = {
    "pin": "1.3.0-alpha.2",
    "versions": ["1.3.0-alpha.5"],
    "named": None,
}

NO_BARE_STABLE = {
    "pin": None,
    "versions": ["1.0.0-nightly", "1.0.0-preview.1"],
}

PLUS_ORDINAL = {
    "pin": "17-ea+9",
    "versions": ["17-ea+10", "17"],
}

JRE_LINES = {
    "pin": "1.3.0-jre8",
    "versions": ["1.3.6", "1.3.6-jre8", "1.3.6-jre-8", "1.4.0-jre11"],
    "named": None,
}

ALIAS_FROM_OLDER = {
    "pin": "0.9.0",
    "versions": ["1.0.0", "1.0.0.Final"],
    "shown": "1.0.0",
}

ALIAS_ALREADY_BARE = {
    "pin": "1.0.0",
    "versions": ["1.0.0", "1.0.0.Final"],
}

DATESTAMP = {
    "pin": "1.0.0",
    "versions": ["1.0.0", "20030203.000550"],
}

ALIAS_ONLY = ["1.0.0.Final", "1.0.0.RELEASE", "1.0.0-ga"]

RELOCATION_POM = (
    "<project><distributionManagement><relocation>"
    "<groupId>com.example</groupId>"
    "<artifactId>lib-new</artifactId>"
    "<version>1.2.3</version>"
    "</relocation></distributionManagement></project>"
)
