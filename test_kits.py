from pathlib import Path
from app.kits import read_kits_xlsx

kits = read_kits_xlsx(Path('templates/kits_template.xlsx'))
assert len(kits) == 60
sample = next(k for k in kits if k.barcode == '2054359985196')
assert sample.components == ('4600987000343', '4600987000343')
print('OK')
