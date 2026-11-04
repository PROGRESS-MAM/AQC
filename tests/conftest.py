import sys
from types import ModuleType


if "toolbox" not in sys.modules:
    toolbox = ModuleType("toolbox")
    toolbox.tb_write_log = lambda *args, **kwargs: None
    sys.modules["toolbox"] = toolbox
