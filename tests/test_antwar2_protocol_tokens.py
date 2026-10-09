"""Wire-format integers must not depend on IntEnum.__str__ across Python versions."""
import importlib
import io
import struct
import sys
from pathlib import Path


def test_upgrade_enum_is_decimal_on_wire():
    root = Path(__file__).resolve().parents[1] / 'games/antwar2/public_sdk'
    prefixes = ('SDK', 'protocol', 'common')
    saved = {k: v for k, v in sys.modules.items() if any(k == p or k.startswith(p + '.') for p in prefixes)}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, str(root))
    try:
        protocol = importlib.import_module('protocol')
        constants = importlib.import_module('SDK.utils.constants')
        model = importlib.import_module('SDK.backend.model')
        output = io.BytesIO()
        protocol.ProtocolIO(stdout=output).send_operations([
            model.Operation(constants.OperationType.UPGRADE_TOWER, 10, constants.TowerType.PRODUCER),
            model.Operation(constants.OperationType.USE_LIGHTNING_STORM, 14, 9),
        ])
        payload = b'2\n12 10 4\n21 14 9\n'
        assert output.getvalue() == struct.pack('>I', len(payload)) + payload
    finally:
        sys.path.remove(str(root))
        for k in list(sys.modules):
            if any(k == p or k.startswith(p + '.') for p in prefixes):
                del sys.modules[k]
        sys.modules.update(saved)
