"""Specific safe diagnostics at preflight and public standalone helper seams."""
import importlib.util
from pathlib import Path
import unittest
import httpx

from studio_platform.generation_diagnostics import preflight_diagnostic, WANGP_VALIDATION_CODES

spec=importlib.util.spec_from_file_location('diagnostic_public_helper',
    Path(__file__).parent/'skills/sixnine-yingxu/scripts/sixnine.py')
helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)


class DiagnosticTests(unittest.TestCase):
    def test_preflight_preserves_known_codes_and_discards_private_or_dynamic_text(self):
        for code in WANGP_VALIDATION_CODES:
            result=preflight_diagnostic(ValueError(code))
            self.assertEqual(result['error_code'],code)
            self.assertEqual(result['error_stage'],'preflight')
        for text in ('wangp_private_token_SECRET','PRIVATE /media/a.png https://x/?token=SECRET',
                'wangp_invalid_inputs SECRET'):
            result=preflight_diagnostic(ValueError(text))
            self.assertEqual(result['error_code'],'preflight_rejected')
            self.assertNotIn(text,str(result))
        result=preflight_diagnostic(ValueError('首尾帧配方不能混用全能参考；请明确切换配方或解除关联'))
        self.assertEqual(result['error_code'],'incompatible_fl_ref_inputs')

    def test_helper_http_failure_exposes_only_known_code_and_stage_and_never_retries(self):
        for code in ('wangp_adapter_ref_first_last_unmapped','wangp_generation_cuda_out_of_memory',
                'incompatible_fl_ref_inputs','wangp_private_token_SECRET'):
            response=httpx.Response(422,json={'code':code,'error_stage':'preflight',
                'message':'SECRET private prompt /media/file https://x/?token=SECRET','token':'SECRET'})
            with self.assertRaises(ValueError) as error:
                helper.check_response(response)
            text=str(error.exception)
            self.assertIn('HTTP 422',text)
            self.assertNotIn('SECRET',text)
            if 'private_token' not in code:
                self.assertIn('code='+code,text)
                self.assertIn('stage=preflight',text)
        for body in ('PRIVATE SECRET','['*20000):
            with self.assertRaises(ValueError) as error:
                helper.check_response(httpx.Response(500,text=body))
            self.assertNotIn(body,str(error.exception))
        with self.assertRaisesRegex(ValueError,'HTTP 503'):
            helper.check_response(httpx.Response(503,stream=httpx.ByteStream(b'PRIVATE SECRET')))
