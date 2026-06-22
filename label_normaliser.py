LABEL_ALIASES = {
    "bit music": "Bit Music",
    "bit music s.a.": "Bit Music",
    "bit music, s.a.": "Bit Music",
    "bit music spain": "Bit Music",

    "max music": "Max Music",
    "max music records": "Max Music",

    "vale music": "Vale Music",
    "vale music s.l.": "Vale Music",
}

def normalise_label(label):
    if not label:
        return ""
    cleaned = label.strip().lower()
    return LABEL_ALIASES.get(cleaned, label.strip())
