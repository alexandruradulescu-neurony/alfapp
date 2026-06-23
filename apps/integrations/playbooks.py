"""Per-site form playbooks: look up the instructions for a form's domain, gather recent
run summaries for a domain, and draft improved instructions from them (AI, human-approved)."""
import logging

from pydantic import BaseModel

from apps.ai.client import AIClient
from apps.integrations.form_fill_service import form_host
from apps.integrations.models import FormFill, FormPlaybook

logger = logging.getLogger(__name__)


def _host_matches(host: str, domain: str) -> bool:
    """A form host matches a playbook domain if equal or a subdomain of it, so a playbook
    keyed 'chargerback.com' covers 'www.chargerback.com' too."""
    return host == domain or host.endswith('.' + domain)


def playbook_for_domain(host: str) -> str:
    """Return the enabled playbook instructions for a form host, or '' if none. Matches by
    domain suffix; if several match, the most specific (longest) domain wins."""
    host = (host or '').strip().lower()
    if not host:
        return ''
    candidates = sorted(FormPlaybook.objects.filter(enabled=True),
                        key=lambda p: -len(p.domain))
    for pb in candidates:
        if _host_matches(host, pb.domain):
            return pb.instructions
    return ''


def recent_run_summaries(domain: str, limit: int = 5) -> list:
    """The agent's end-of-run summaries for recent fills on this form domain, newest first.
    These already report what got filled and where the agent got stuck — the raw material
    the AI improves the playbook from. Empty summaries are skipped."""
    domain = (domain or '').strip().lower()
    if not domain:
        return []
    qs = (FormFill.objects.exclude(result_output='')
          .filter(form_url__icontains=domain)
          .order_by('-updated_at', '-id'))
    out = []
    for ff in qs:
        if _host_matches(form_host(ff.form_url), domain):
            out.append(ff.result_output)
            if len(out) >= limit:
                break
    return out


class _Suggestion(BaseModel):
    instructions: str = ''


SUGGEST_PROMPT = (
    "You improve a per-site instruction set for an automated agent that fills airport "
    "lost-and-found report forms. From the current instructions and the agent's reports of "
    "recent real runs, write a SHARPER instruction set for this form: concise, imperative, "
    "site-specific tips — how to operate dropdowns and pop-up pickers, which required "
    "checkboxes to tick, what to leave blank, fields that consistently caused trouble. Keep "
    "what still applies, add what the runs revealed, drop nothing useful. Output instructions only."
)


def suggest_playbook_instructions(current: str, summaries: list) -> str:
    """Draft improved playbook instructions from recent run summaries. '' if there's nothing
    to learn from or the AI call fails (the caller surfaces a message; nothing auto-saves)."""
    if not summaries:
        return ''
    try:
        out = AIClient().complete(
            system_prompt=SUGGEST_PROMPT,
            trusted={'current_instructions': current or '(none yet)',
                     'recent_runs': '\n---\n'.join(summaries)},
            response_schema=_Suggestion,
            call_site='form_fill_playbook_suggest',
        )
        return (out.instructions or '').strip()
    except Exception as e:   # noqa: BLE001
        logger.warning('Playbook suggestion failed for runs (%d summaries): %s', len(summaries), e)
        return ''
