import test from 'node:test';
import assert from 'node:assert/strict';
import {jobMessage,capacityWaitMessage} from './cloud-model.js';

test('capacity wait explains inventory, billing uncertainty and preparation separately',()=>{
  const waiting=error_code=>({status:'waiting_capacity',error_code});
  assert.match(jobMessage(waiting('capacity_no_matching_gpu')),/没有符合.*可用 GPU/);
  assert.match(jobMessage(waiting('capacity_inventory_check_failed')),/无法确认 GPU 库存/);
  assert.match(jobMessage(waiting('capacity_rental_reconciliation')),/暂停再次租机/);
  assert.match(jobMessage(waiting('capacity_gpu_starting')),/加载模型/);
  assert.match(jobMessage(waiting('capacity_budget_or_limit')),/不会自动提高预算/);
  assert.match(jobMessage(waiting('unrecognized')),/等待计算容量/);
  assert.equal(capacityWaitMessage({status:'succeeded',error_code:'capacity_no_matching_gpu'}),'');
});
