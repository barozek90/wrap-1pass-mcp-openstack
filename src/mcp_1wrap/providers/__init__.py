"""Provider registry."""

from mcp_1wrap.providers import envvars, openstack

REGISTRY = {
    openstack.NAME: openstack,
    envvars.NAME: envvars,
}
