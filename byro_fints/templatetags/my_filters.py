import re
from django.template.defaultfilters import register
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe


@register.filter(name="lookup")
def lookup(data, key):
    return data.get(key, "")


@register.filter(name="format_iban")
def format_iban(iban):
    return mark_safe(
        "&nbsp;".join(
            conditional_escape(e)
            for e in re.findall(r".{,4}", re.sub(r"\s", "", iban))
            if e
        )
    )
