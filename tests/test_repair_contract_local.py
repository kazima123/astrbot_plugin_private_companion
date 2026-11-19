"""Repair must preserve item identity and complete no-change decisions without fabricating facts."""
import sys,json,unittest
from companion_v2.execution.models import encode
from tests.test_memory_recovery import MemoryRecoveryTests
from tests.test_host_application import HostFixture
from tests.test_role_tool_loop import answered

class RepairContractTests(HostFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        from companion_v2.configuration.registry import FieldRegistry
        from companion_v2.memory.reflection import reflection_profile
        from companion_v2.model_gateway.routing import configuration_fields
        self.profiles += (reflection_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="repair-contract-test.v1", fields=configuration_fields(self.profiles))
    setup_reflection = MemoryRecoveryTests.setup_reflection
    configure = MemoryRecoveryTests.configure
    setup_manual = MemoryRecoveryTests.setup_manual

    async def test_omitted_item_is_named_in_instruction_and_noop_resolves_ancestors(self):
        host, context, prepared = await self.setup_manual()
        reflection = host.application.memory_reflection
        original = self.model_behavior
        async def bad(envelope):
            output = json.loads((await original(envelope)).draft.text)
            output['proposals'][0]['proposal']['confidence'] = '0.7'
            return answered(encode(output))
        self.model_behavior = bad
        result = (await reflection.run(context, prepared['batch_id'])).unwrap()
        self.assertEqual(result['state'], 'partial')
        first = reflection.retry(context, prepared['batch_id'], request_id='omit').unwrap()
        async def omit(envelope):
            return answered(encode({'summary':'不新增。','proposals':[]}))
        self.model_behavior = omit
        self.assertEqual((await reflection.run(context, first['batch_id'])).unwrap()['state'], 'partial')
        second = reflection.retry(context, first['batch_id'], request_id='resolve').unwrap()
        async def resolve(envelope):
            self.assertIn('["preference"]', envelope.original.instruction)
            return answered(encode({'summary':'来源无需新增长期事实。','proposals':[{
                'item_id':'preference','operation':'no_op','evidence_handles':[],
                'predicate':None,'object':None,'representation':None,'target':None,
                'details':[{'field':'reason','value':'无需新增事实。'}]}]}))
        self.model_behavior = resolve
        result = (await reflection.run(context, second['batch_id'])).unwrap()
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(reflection.status(context, prepared['batch_id']).unwrap()['state'], 'complete')
        self.assertEqual(len(host.application.runtime.read_all('SELECT * FROM memory_atoms', ())), 0)
        count = len(self.called)
        await reflection.run(context, second['batch_id'])
        self.assertEqual(len(self.called), count)

    async def test_invalid_json_repair_keeps_original_allowed_item_ids(self):
        host, context, prepared = await self.setup_manual()
        reflection = host.application.memory_reflection
        original = self.model_behavior
        async def bad(envelope):
            output=json.loads((await original(envelope)).draft.text)
            output['proposals'][0]['proposal']['confidence']='0.7'
            return answered(encode(output))
        self.model_behavior=bad
        await reflection.run(context, prepared['batch_id'])
        first=reflection.retry(context, prepared['batch_id'], request_id='malformed').unwrap()
        async def malformed(envelope):
            return answered('{"summary":')
        self.model_behavior=malformed
        self.assertEqual((await reflection.run(context, first['batch_id'])).unwrap()['state'],'failed')
        second=reflection.retry(context, first['batch_id'], request_id='keep-scope').unwrap()
        manifest=json.loads(reflection._row(context, second['batch_id'])['manifest_json'])
        self.assertEqual(manifest.get('repair_item_ids'), ['preference'])
        self.assertEqual(manifest['repair_material']['allowed_item_ids'], ['preference'])

if __name__ == '__main__':
    unittest.main()
