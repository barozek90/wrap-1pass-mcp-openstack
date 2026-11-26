"""Provider registry."""

from mcp_1wrap.providers import envvars, item, openstack

REGISTRY = {
    openstack.NAME: openstack,
    envvars.NAME: envvars,
    item.NAME: item,
}
