"""Per-site form playbooks: look up the instructions for a form's domain, gather recent
run summaries for a domain, and (Phase C) draft improved instructions from them."""
from apps.integrations.models import FormPlaybook


def playbook_for_domain(domain: str) -> str:
    """Return the enabled playbook instructions for a form host, or '' if none."""
    domain = (domain or '').strip().lower()
    if not domain:
        return ''
    pb = FormPlaybook.objects.filter(domain=domain, enabled=True).first()
    return pb.instructions if pb else ''
