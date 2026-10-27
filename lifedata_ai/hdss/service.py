"""Saving, reading back and authorising template dashboards: hdss-site and hdss-master (views and Vima's tools)."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lifedata_ai.hdss.spec import PRESET, SpecError, build_spec, effective, validate_spec
from lifedata_ai.superset_client import SupersetError


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise SpecError('That is not a dashboard id.') from exc


MASTER_PRESET = 'hdss-master'


def is_hdss(publication) -> bool:
    """A template page (hdss-site or hdss-master)."""
    return (publication.configuration or {}).get('preset') in (PRESET, MASTER_PRESET)


def is_master(spec_or_publication) -> bool:
    spec = spec_or_publication if isinstance(spec_or_publication, dict) else (spec_or_publication.configuration or {}).get('spec') or {}
    return spec.get('template') == MASTER_PRESET


def visible_to(publication, identity) -> bool:
    """Drafts are visible only to their owner; published template pages follow normal access rules."""
    if not is_hdss(publication):
        return True
    spec = publication.configuration.get('spec') or {}
    return spec.get('status') == 'published' or publication.owner_username == identity.username


def authorise(client, publication) -> None:
    """The viewer must be able to read the bound dataset in Superset (raises SupersetError otherwise)."""
    spec = publication.configuration.get('spec') or {}
    ids = (spec.get('source') or {}).get('datasets') if is_master(spec) else [(spec.get('dataset') or {}).get('superset_dataset_id')]
    if not ids or any(type(i) is not int for i in ids):
        raise SupersetError('Publication has no sources.', status_code=404)
    def check(dataset_id):
        if not client.get_dataset(dataset_id):
            raise SupersetError('Dataset not found.', status_code=404)
    # Master sources can bind many datasets. Check each on every request, but
    # avoid serial HTTP round trips before serving an in-memory filter result.
    with ThreadPoolExecutor(max_workers=min(4, len(ids))) as pool:
        list(pool.map(check, dict.fromkeys(ids)))


def save(client, username: str, spec: dict[str, Any], publication_id: str | None = None) -> dict[str, Any]:
    """Validate, save as given (draft unless already published by its owner), re-read and re-validate."""
    from lifedata_ai.models import DashboardPublication
    if is_master(spec):
        from lifedata_ai.hdss import master
        src, cov = master.validate_master_spec(spec, client)
        label, synthetic, report, unbound, preset = src['label'], src['synthetic'], cov, master.unbound_summary(cov), MASTER_PRESET
        revalidate = master.validate_master_spec
    else:
        binding, report = validate_spec(spec, client)
        label, synthetic, unbound, preset, revalidate = binding.label, binding.synthetic, unbound_summary(report), PRESET, validate_spec
    configuration = {'preset': preset, 'spec': spec}
    if publication_id:
        publication = DashboardPublication.objects.filter(pk=_uuid(publication_id), owner_username=username).first()
        if publication is None or not is_hdss(publication):
            raise SpecError('No HDSS dashboard with that id is owned by you.')
        publication.title, publication.configuration = spec['title'], configuration
        publication.save(update_fields=['title', 'configuration', 'updated_at'])
    else:
        publication = DashboardPublication.objects.create(
            superset_dashboard_id=None, owner_username=username, title=spec['title'], theme='light',
            configuration=configuration, layout=[])
    stored = DashboardPublication.objects.get(pk=publication.pk)
    verified = stored.configuration.get('spec') == spec
    if verified:
        revalidate(stored.configuration['spec'])
    return {'publication_id': str(stored.pk), 'url': f'/lifedata-ai/dashboards/{stored.pk}/',
            'status': spec['status'], 'verified': verified, 'dataset': label,
            'synthetic': synthetic, 'coverage': report, 'unbound': unbound}


def create(client, username: str, dataset_key: str, title: str, deep_dive: dict | None = None,
           slots: list | None = None, filters: list | None = None, population_reference: int | None = None) -> dict[str, Any]:
    from lifedata_ai.hdss.registry import load_bindings
    binding = load_bindings().get(dataset_key)
    if binding is None:
        raise SpecError(f'{dataset_key!r} is not a registered HDSS dataset. Registered: {sorted(load_bindings())}.')
    return save(client, username, build_spec(binding, title, deep_dive, slots, filters, population_reference))


def create_master(client, username: str, source_key: str, title: str, publication_id: str | None = None) -> dict[str, Any]:
    """A new Master draft, or (publication_id) a rebuild of one the user owns, e.g. after a registry change."""
    from lifedata_ai.hdss import master
    return save(client, username, master.build_master_spec(source_key, title), publication_id)


def set_status(username: str, publication_id: str, status: str) -> dict[str, Any]:
    from lifedata_ai.models import DashboardPublication
    if status not in ('draft', 'published'):
        raise SpecError('Status must be draft or published.')
    publication = DashboardPublication.objects.filter(pk=_uuid(publication_id), owner_username=username).first()
    if publication is None or not is_hdss(publication):
        raise SpecError('No HDSS dashboard with that id is owned by you.')
    spec = dict(publication.configuration['spec'], status=status)
    if is_master(spec):
        from lifedata_ai.hdss import master
        master.validate_master_spec(spec)
    else:
        validate_spec(spec)
    publication.configuration = {**publication.configuration, 'spec': spec}
    publication.save(update_fields=['configuration', 'updated_at'])
    stored = DashboardPublication.objects.get(pk=publication.pk)
    return {'publication_id': str(stored.pk), 'status': stored.configuration['spec']['status'],
            'verified': stored.configuration['spec']['status'] == status}


def page_spec(publication) -> dict[str, Any]:
    """What the browser receives: the effective spec (unbound slots dropped, reasons listed)."""
    from lifedata_ai.hdss.registry import binding_for_dataset
    spec = publication.configuration['spec']
    if is_master(spec):
        from lifedata_ai.hdss import master
        return master.page_spec(spec)
    binding = binding_for_dataset(spec['dataset']['superset_dataset_id'])
    if binding is None:
        raise SpecError('This dashboard\'s dataset is no longer registered for the HDSS template.')
    out = effective(spec, binding)
    out['labels'] = {name: dim.labels for name in spec['dimensions'] if (dim := binding.dimension(name))}
    out['unbound_categories'] = {name: [label for label, _ in dim.unbound_categories]
                                 for name in spec['dimensions'] if (dim := binding.dimension(name)) and dim.unbound_categories}
    out['reference_date'] = binding.reference_date
    return out


def unbound_summary(report: dict[str, Any]) -> list[str]:
    lines = []
    for group in ('slots', 'filters'):
        for name, value in report[group].items():
            if value == 'bound':
                continue
            if value.get('status') == 'partial':
                for part, why in value['hidden'].items():
                    lines.append(f"{name}: {part} hidden ({why['status']}: {why['reason']})")
            elif 'parts' in value:
                for part, why in value['parts'].items():
                    lines.append(f"{name}: {part} ({why['status']}: {why['reason']})")
            else:
                lines.append(f"{name}: {value['status']}: {value['reason']}")
    return lines
