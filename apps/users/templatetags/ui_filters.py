from django import template

register = template.Library()


@register.filter
def humanize_label(value):
    """Convert UPPER_SNAKE_CASE to Title Case.
    e.g. GENERAL_CORRESPONDENCE -> General Correspondence
         OBJECT_NOT_FOUND -> Object Not Found
    """
    if not value:
        return value
    return str(value).replace("_", " ").title()
