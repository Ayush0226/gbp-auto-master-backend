"""Export the entire registered tool contract from the runtime schemas."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mcp_server import create_gbp_mcp_server


async def export():
    tools = await create_gbp_mcp_server(MagicMock()).list_tools()
    document = {'schema_version':'3.0.0','source':'registered runtime tool schemas',
        'usage_credits':False,'tools':[{ 'name':tool.name,'description':tool.description,
            'inputSchema':tool.input_schema,'outputSchema':tool.output_schema,
            'annotations':tool.annotations.model_dump(by_alias=True,exclude_none=True),
        } for tool in tools]}
    path=Path(__file__).resolve().parents[1]/'docs'/'gbp-master-mcp-tools.json'
    path.write_text(json.dumps(document,indent=2),encoding='utf-8')
    print(f'Exported {len(tools)} tools')


if __name__=='__main__': asyncio.run(export())
