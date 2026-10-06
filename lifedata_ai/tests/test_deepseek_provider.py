"""DeepSeek preserves tool execution and streaming while using its own API key."""
import json
import httpx
import pytest
from lifedata_ai.agent.deepseek_provider import DeepSeekClient, compatible_messages
from lifedata_ai.agent.loop import _provider_message, run_turn
from lifedata_ai.models import Message


def message_events():
    events = [
        {'type':'message_start','message':{'id':'msg_test','type':'message','role':'assistant','content':[],
         'model':'deepseek-flash','stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}},
        {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
        {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Ready'}},
        {'type':'content_block_stop','index':0},
        {'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}},
        {'type':'message_stop'},
    ]
    return ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events)


def test_deepseek_boundary_keeps_tool_pairs_and_marks_unsupported_pdf():
    messages = [{'role':'assistant','content':[{'type':'thinking','thinking':'reason','signature':'foreign'},
        {'type':'redacted_thinking','data':'hidden'}, {'type':'tool_use','id':'call1','name':'read','input':{}}]},
        {'role':'user','content':[{'type':'tool_result','tool_use_id':'call1','content':'result'},
        {'type':'document','source':{'type':'base64','media_type':'application/pdf','data':'pdf'}},
        {'type':'image','source':{'type':'base64','media_type':'image/png','data':'image'}}]}]
    result = compatible_messages(messages)
    assert result[0]['content'][0] == {'type':'thinking','thinking':'reason'}
    assert result[0]['content'][1]['id'] == result[1]['content'][0]['tool_use_id']
    assert 'Do not claim to have read it' in result[1]['content'][1]['text']
    assert result[1]['content'][2]['type'] == 'image'
    assert messages[0]['content'][0]['signature'] == 'foreign'


def test_deepseek_stream_uses_correct_endpoint_key_and_effort(respx_mock):
    route = respx_mock.post('https://api.deepseek.com/anthropic/v1/messages').mock(
        return_value=httpx.Response(200, text=message_events(), headers={'content-type':'text/event-stream','request-id':'req_test'}))
    client = DeepSeekClient('fake-deepseek')
    with client.messages.stream(model='deepseek-flash',max_tokens=200,system=[{'type':'text','text':'system','cache_control':{'type':'ephemeral'}}],
        messages=[{'role':'user','content':[{'type':'text','text':'Hello'}]}],tools=[],output_config={'effort':'low'},timeout=10) as stream:
        chunks=list(stream);final=stream.get_final_message()
    assert final.content[0].text == 'Ready'
    assert stream.request_id == 'req_test'
    request=route.calls.last.request
    assert request.headers['x-api-key'] == 'fake-deepseek'
    body=json.loads(request.content)
    assert body['model']=='deepseek-flash' and body['output_config']=={'effort':'low'}
    assert 'cache_control' not in body['system'][0]
    assert any(c.type=='content_block_delta' for c in chunks)


@pytest.mark.django_db
def test_deepseek_turn_streams_through_existing_agent(settings,conversation,analyst_identity,page_context,respx_mock):
    settings.LIFEDATA_AI_PROVIDER='deepseek';settings.DEEPSEEK_API_KEY='fake-deepseek'
    settings.ANTHROPIC_API_KEY='';settings.OPENAI_API_KEY=''
    route=respx_mock.post('https://api.deepseek.com/anthropic/v1/messages').mock(
        return_value=httpx.Response(200,text=message_events(),headers={'content-type':'text/event-stream'}))
    events=list(run_turn(conversation=conversation,user_message='Hello',identity=analyst_identity,
        page_context=page_context,superset_session_cookie='fake'))
    assert route.called
    assert any(e.name=='text_delta' and e.data['text']=='Ready' for e in events)
    assert events[-1].data['stop_reason']=='end_turn'
    assert conversation.messages.filter(role=Message.Role.ASSISTANT).last().content==[{'type':'text','text':'Ready'}]


def test_deepseek_auth_and_balance_errors_are_actionable():
    from lifedata_ai.agent.openai_provider import OpenAIStatusError
    assert 'DEEPSEEK_API_KEY' in _provider_message(OpenAIStatusError(401,'Rejected'),'deepseek')
    assert 'insufficient balance' in _provider_message(OpenAIStatusError(402,'Insufficient Balance'),'deepseek')
