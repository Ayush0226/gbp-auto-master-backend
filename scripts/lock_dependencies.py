"""Record the installed runtime dependency graph, excluding old/dev packages."""
from importlib import metadata
from pathlib import Path
from packaging.requirements import Requirement

root=Path(__file__).resolve().parents[1]
pending=[Requirement(line.strip()) for line in (root/'requirements.txt').read_text().splitlines()
    if line.strip() and not line.lstrip().startswith(('#','-'))]
seen=set(); versions={}
while pending:
    requirement=pending.pop()
    name=requirement.name.lower().replace('_','-')
    visit=(name,tuple(sorted(requirement.extras)))
    if visit in seen: continue
    seen.add(visit)
    dist=metadata.distribution(name)
    versions[name]=dist.version
    for raw in dist.requires or []:
        child=Requirement(raw)
        if child.marker is None or any(child.marker.evaluate({'extra':extra}) for extra in (requirement.extras or {''})):
            pending.append(child)
lines=['# Installed runtime constraints. Regenerate after reviewing dependency updates.']
for name,version in sorted(versions.items()):
    marker='; sys_platform == "win32"' if name=='pywin32' else ''
    lines.append(f'{name}=={version}{marker}')
(root/'requirements-lock.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(f'Locked {len(versions)} runtime packages')
