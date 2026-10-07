"""Lossless transport encoding for malformed historical function-call arguments.

The original call text and failed tool response remain visible. No tool is executed
and no strategy, budget, or stored native conversation is edited.
"""
import json
KEY='__aa_arena_original_invalid_json_arguments__'
def normalize(request):
 original=request.get('input')
 if not isinstance(original,list):return request,[]
 items=list(original);changed=[]
 for i,item in enumerate(original):
  if not isinstance(item,dict) or item.get('type')!='function_call' or not isinstance(item.get('arguments'),str):continue
  raw=item['arguments']
  try:json.loads(raw)
  except json.JSONDecodeError:
   items[i]=dict(item,arguments=json.dumps({KEY:raw},ensure_ascii=False));changed.append({'index':i,'name':item.get('name'),'call_id':item.get('call_id')})
 return (dict(request,input=items),changed) if changed else (request,[])
