"""Keep solver networks internal and bind their host ports to loopback."""

LOOPBACK_BRIDGE = {"com.docker.network.bridge.host_binding_ipv4": "127.0.0.1"}


def safe_agent_network(compose, name):
    compose["networks"]["default"]["internal"] = True
    compose["networks"]["agent"] = {
        "name": name, "internal": True, "driver_opts": LOOPBACK_BRIDGE,
    }
    for name in ("elt-api", "elt-files"):
        compose["services"][name]["networks"] = ["default", "agent"]
