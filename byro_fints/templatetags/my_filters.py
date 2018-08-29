from django.template.defaultfilters import register

@register.filter(name='lookup')
def lookup(data, key):
    return data.get(key, '')
