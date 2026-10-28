from dataclasses import dataclass, field
from typing import List
@dataclass
class LocalAnswer:
    subject: str = ''
    text: str = ''
    routine: str = ''
    procedure: str = ''
    def as_dict(self):
        return vars(self).copy()
class Spark:
    pass
class CliSpark(Spark):
    pass
PROBES = ()
