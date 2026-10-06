"""Publish through the chatbot's audited publish_dashboard tool, without a model call."""

import json
import subprocess
from pathlib import Path


def main():
    path = Path("/tmp/lifedata-population-build.json")
    manifest = json.loads(path.read_text())
    session = json.loads(Path("/tmp/lifedata-author-session.json").read_text())
    arguments = {
        "dashboard_id": manifest["dashboards"]["discovery"],
        "title": "Sandbox · Population discovery",
        "theme": "light",
        "configuration": {
            "preset": "population-discovery",
            "source_chart_id": manifest["charts"]["source"],
            "boundary_chart_id": manifest["charts"]["boundaries"],
            "facility_chart_id": manifest["charts"]["facilities"],
            "summary_chart_id": manifest["charts"]["summary"],
            "default_facility": manifest["default_facility"],
            "region_label": "Buikwe District",
            "native_dashboard_ids": [
                manifest["dashboards"][k] for k in ("overview", "discovery", "profile", "list")
            ],
        },
    }
    script = """import os,json,uuid
os.environ.setdefault('DJANGO_SETTINGS_MODULE','lifedata.settings')
import django;django.setup()
from lifedata_ai.models import Conversation,Message
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.superset_client import SupersetClient
from lifedata_ai.tools import ToolContext,execute
arguments=ARGUMENTS
client=SupersetClient(SESSION)
me=client.me()
identity=SupersetIdentity(username=me['username'],user_id=me['id'],roles=list(client.my_roles()))
conversation=Conversation.objects.create(superset_username=identity.username,title='Build Sandbox population discovery',page_scope={'page_type':'dashboard','dashboard_id':arguments['dashboard_id']})
Message.objects.create(conversation=conversation,role=Message.Role.USER,content=[{'type':'text','text':'Create new Superset Sandbox dashboards from the existing Sandbox join and publish the matching custom population-discovery UI.'}])
call_id='toolu_publish_'+uuid.uuid4().hex
Message.objects.create(conversation=conversation,role=Message.Role.ASSISTANT,content=[{'type':'tool_use','id':call_id,'name':'publish_dashboard','input':arguments}])
context=ToolContext(superset_session_cookie=SESSION,identity=identity,conversation=conversation,page_context=conversation.page_scope)
result=execute('publish_dashboard',arguments,context)
Message.objects.create(conversation=conversation,role=Message.Role.TOOL,content=[result.to_tool_result_block(call_id)])
if not result.ok:raise RuntimeError(result.error)
Message.objects.create(conversation=conversation,role=Message.Role.ASSISTANT,content=[{'type':'text','text':'Published the new Sandbox population-discovery dashboard: '+result.content['url']}])
print(json.dumps({**result.content,'conversation_id':str(conversation.id)}))
""".replace("ARGUMENTS", repr(arguments)).replace("SESSION", repr(session["session"]))
    result = subprocess.run(
        ["docker", "exec", "-i", "lifedata-ai4cti", "/app/.venv/bin/python", "-"],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        print(result.stderr.replace(session["session"], "[REDACTED]"))
        raise SystemExit(result.returncode)
    publication = json.loads(result.stdout)
    manifest["publication"] = publication
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(publication, indent=2))


if __name__ == "__main__":
    main()
