import os
import json
import requests
import sys
import urllib.parse
from typing import Dict, Any, List, Tuple, Optional

from nanobot.agent.tools.base import Tool


def get_tenant_access_token(app_id: str, app_secret: str) -> Tuple[str, Optional[Exception]]:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": app_id, "app_secret": app_secret}
    headers = {"Content-Type": "application/json; charset=utf-8"}
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        if result.get("code", 0) != 0:
            return "", Exception(f"failed to get token: {result.get('msg')}")
        return result["tenant_access_token"], None
    except Exception as e:
        return "", e

def get_wiki_node_info(tenant_access_token: str, node_token: str) -> Dict[str, Any]:
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={urllib.parse.quote(node_token)}"
    headers = {"Authorization": f"Bearer {tenant_access_token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    result = response.json()
    return result.get("data", {}).get("node", {})

def parse_base_url(tenant_access_token: str, base_url_string: str) -> Dict[str, Optional[str]]:
    from urllib.parse import urlparse, parse_qs
    parsed_url = urlparse(base_url_string)
    pathname = parsed_url.path
    app_token = pathname.split("/")[-1]
    if "/wiki/" in pathname:
        node_info = get_wiki_node_info(tenant_access_token, app_token)
        if node_info.get("obj_type") == "bitable":
            app_token = node_info.get("obj_token", app_token)
    query_params = parse_qs(parsed_url.query)
    return {
        "app_token": app_token,
        "table_id": query_params.get("table", [None])[0],
        "view_id": query_params.get("view", [None])[0]
    }

def list_bitable_tables(tenant_access_token: str, app_token: str) -> List[Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    headers = {"Authorization": f"Bearer {tenant_access_token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("data", {}).get("items", [])

def search_bitable_records(
    tenant_access_token: str,
    app_token: str,
    table_id: str, 
    view_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
    headers = {"Authorization": f"Bearer {tenant_access_token}"}
    payload = {"page_size": 100} 
    if view_id: payload["view_id"] = view_id
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json().get("data", {}).get("items", [])



class Feishu_ReadBiTable_Tool(Tool):
    """
    Read Feishu BiTable
    """
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        
    @property
    def name(self) -> str:
        return "get_feishu_bitable_data"

    @property
    def description(self) -> str:
        return (
            "Access Feishu BiTable"
            "Get data according to the given table name"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "URL of Feishu BiTable."
                },
            },
            "required": ["base_url"]
        }

    async def execute(
        self, 
        base_url: str, 
        **kwargs: Any
    ) -> str:
        # authentication
        token, err = get_tenant_access_token(self.app_id, self.app_secret)
        if err:
            return f"Error: Authentication - {str(err)}"

        try:
            # parse base url
            params = parse_base_url(token, base_url)
            app_token = params["app_token"]
            table_id = params["table_id"]
            view_id = params["view_id"]

            # search
            records = search_bitable_records(token, app_token, table_id, view_id)
            
            if not records:
                return "表格解析成功，但表中没有找到任何记录。"

            # format
            formatted_data = []
            for rec in records: 
                formatted_data.append(rec.get("fields", {}))

            output = {
                "metadata": {
                    "app_token": app_token, 
                    "table_id": table_id, 
                    "total": len(records)
                },
                "data": formatted_data
            }
            
            return json.dumps(output, ensure_ascii=False, indent=2)

        except Exception as e:
            return f"Error: Implementation error - {str(e)}"




# offline testing code
if __name__ == "__main__":
    # 模拟框架调用逻辑
    import asyncio
    tool = Feishu_ReadBiTable_Tool(
        app_id="cli_a905f5109fb8dcc0",
        app_secret="ucEMjWiIZojYycJ0VSI02gJm3XO4w4vG"
    )
    # 填入你实际想测试的 URL
    test_url = "https://wcnu7o3ua7by.feishu.cn/wiki/ND7KwImwii5uPZkW8tscMWQEntc?table=tblZ2AUbMqwMsqD1"
    
    async def test():
        result = await tool.execute(
            base_url=test_url,
            app_id="cli_a905f5109fb8dcc0",
            app_secret="ucEMjWiIZojYycJ0VSI02gJm3XO4w4vG"
        )
        print(result)
    
    asyncio.run(test())
    