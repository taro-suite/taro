import importlib
import logging
import pkgutil
from inspect import signature
from logging import INFO

import taro.log

log = logging.getLogger(__name__)


def discover_plugins(prefix, names):
    discovered = {
        name: importlib.import_module(name)
        for finder, name, ispkg
        in pkgutil.iter_modules()
        if name.startswith(prefix)
    }
    log.debug("event=[plugin_discovered] plugins=[%s]", ",".join(discovered.keys()))

    module2listener = {}
    for name in names:
        if name not in discovered.keys():
            log.warning("event=[plugin_not_found] plugin=[%s]", name)
            continue

        try:
            module = discovered[name]
            listener = load_plugin(module)
            module2listener[module] = listener
        except Exception as e:
            log.warning("event=[invalid_plugin] plugin=[%s] reason=[%s]", name, e)

    return module2listener


def load_plugin(plugin_module):
    listener = plugin_module.create_listener()  # Raises AttributeError if not 'create_listener' method
    if not listener:
        raise ValueError("listener cannot be None")
    notify_method = listener.notify  # Raises AttributeError if not 'notify' method
    if not callable(notify_method):
        raise AttributeError("plugin listener {} has no method 'notify'".format(listener))
    notify_sig = signature(notify_method)
    if len(notify_sig.parameters) != 1:
        raise AttributeError("plugin listener {} must have method 'notify' with one parameter".format(listener))

    return listener
