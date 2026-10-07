import unittest,json,copy
from aa_arena.benchmark.history_encoding import normalize,KEY
class EncodingTests(unittest.TestCase):
 def test_malformed_history_remains_losslessly_visible_and_not_executed(self):
  raw='{"plan":[{"non improving", "status":"completed"}]}'
  request={'input':[{'type':'function_call','name':'update_plan','call_id':'c','arguments':raw},{'type':'function_call_output','call_id':'c','output':'Error parsing function arguments'}],'reasoning':{'effort':'max'}};before=copy.deepcopy(request)
  fixed,changes=normalize(request);self.assertEqual(request,before);self.assertEqual(json.loads(fixed['input'][0]['arguments'])[KEY],raw);self.assertEqual(fixed['input'][1],before['input'][1]);self.assertEqual(len(changes),1);self.assertEqual(normalize(fixed)[1],[])
 def test_valid_and_freeform_calls_are_untouched(self):
  request={'input':[{'type':'function_call','arguments':'{ "a": 1 }'},{'type':'custom_tool_call','input':'not JSON'}]}
  fixed,changes=normalize(request);self.assertIs(fixed,request);self.assertEqual(changes,[])
if __name__=='__main__':unittest.main()
