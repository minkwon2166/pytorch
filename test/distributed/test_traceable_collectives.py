# Owner(s): ["module: dynamo"]
import unittest
import torch
from torch._dispatch.python import enable_python_dispatcher
import torch._dynamo
import torch._dynamo.test_case
import torch.distributed as dist
from torch._dynamo.utils import same
from torch._dynamo.testing import CompileCounter
from torch.distributed.distributed_c10d import _get_default_group
from torch._C._distributed_c10d import _register_process_group
from torch.fx.experimental.proxy_tensor import make_fx
from torch.testing._internal.common_distributed import (
    _dynamo_dist_per_rank_init,
    DynamoDistributedSingleProcTestCase,
    DynamoDistributedMultiProcTestCase,
    skip_if_lt_x_gpu,
    requires_nccl
)
from torch._inductor.compile_fx import compile_fx as inductor_compile_fx
import torch._dynamo.logging

# LOL if you don't remember to import this, then the op isn't registered and it hits
# the no-op C++ kernel that i am forced to implement despite not using it
import torch.distributed.traceable_collectives


@requires_nccl()
class TestCollectivesMultiProc(DynamoDistributedMultiProcTestCase):
    """
    Run correctness checks in multi-proc runner, mark with minimum # GPUs to run under
    """

    @unittest.skip("hangs in nccl somewhere. work cleanup issue?")
    @skip_if_lt_x_gpu(2)
    def test_allreduce_inductor(self):
        """
        This is matmul/cat/allreduce is a pattern we aim to optimize.
        """

        def matmul_cat_col(a, b, c, d, e, f, *, pg_id):
            x = torch.matmul(a, b)
            y = torch.matmul(c, d)
            z = torch.cat((x, y))
            ar = torch.ops.aten.all_reduce(z, group_id=pg_id, reduce_op="sum")
            g = torch.matmul(e, f)
            out = torch.add(ar, g.repeat(2, 1))
            return (out, )

        def compile(func, example_inputs):
            graph = make_fx(func)(*example_inputs)
            return inductor_compile_fx(graph, example_inputs)

        with _dynamo_dist_per_rank_init(self.rank, self.world_size):

            pg = _get_default_group()
            pg_id = _register_process_group(pg)
            matmul_cat_col = functools.partial(matmul_cat_col, pg_id=pg_id)
            inputs = (torch.ones(4, 4, device="cuda") + self.rank,) * 6

            # non-ideally, i seem to need to enable this at user level in order to construct a torchdispatch subclass
            # inside py registered collective ops
            with enable_python_dispatcher():
                eager_out = matmul_cat_col(*inputs)
                compiled_matmul_cat_col = compile(matmul_cat_col, inputs)
                inductor_out = compiled_matmul_cat_col(*inputs)
                assert same(eager_out, inductor_out)

    @skip_if_lt_x_gpu(2)
    def test_allreduce_eager(self):
        with _dynamo_dist_per_rank_init(self.rank, self.world_size):
            pg = _get_default_group()
            pg_id = _register_process_group(pg)
            input = torch.ones(4, 4, device="cuda")
            orig_input = input.clone()

            with enable_python_dispatcher():
                correct = input.clone()
                dist.all_reduce(correct, async_op=False)

                out = torch.ops.aten.all_reduce(input, group_id=pg_id, reduce_op="sum")
                assert same(correct, out), f"aten.all_reduce ({out}) didn't match dist.all_reduce ({correct})!"
                assert same(orig_input, input), "aten.all_reduce mutated input!"


@requires_nccl()
class TestCollectives(DynamoDistributedSingleProcTestCase):
    """
    Prefer single-proc test runner for basic tests as it is easier to work with.
    """

    def test_inductor_single_op(self):
        torch._inductor.config.debug = True

        def func(inp, *, pg_id):
            ar = torch.ops.aten.all_reduce(inp, group_id=pg_id, reduce_op="sum")
            return ar

        pg = _get_default_group()
        pg_id = _register_process_group(pg)
        inputs = torch.ones(4, 4, device="cuda")

        with enable_python_dispatcher():
            compiled = torch.compile(func)
            out = compiled(inputs, pg_id=pg_id)
            correct = func(inputs, pg_id=pg_id)
            assert same(out, correct)

    def test_inductor_steal_buffer(self):
        """
        it's ok and optimal if inductor allreduce mutates the buffer of an intermediate
        that isn't going to be used again
        """
        torch._inductor.config.debug = True

        def func(inp, *, pg_id):
            x = inp + 1
            ar = torch.ops.aten.all_reduce(x, group_id=pg_id, reduce_op="sum")
            return ar

        pg = _get_default_group()
        pg_id = _register_process_group(pg)
        inputs = torch.ones(4, 4, device="cuda")

        with enable_python_dispatcher():
            compiled = torch.compile(func)
            out = compiled(inputs, pg_id=pg_id)
            correct = func(inputs, pg_id=pg_id)
            assert same(out, correct)

    def test_inductor_doesnt_mutate_shared(self):
        """
        make sure that an intermediate that's going to be reuse isn't mutated unless copied
        """
        torch._inductor.config.debug = True

        def func(inp, *, pg_id):
            x = inp + 1
            ar = torch.ops.aten.all_reduce(x, group_id=pg_id, reduce_op="sum")
            y = x + 2
            return ar, y

        pg = _get_default_group()
        pg_id = _register_process_group(pg)
        inputs = torch.ones(4, 4, device="cuda")

        with enable_python_dispatcher():
            compiled = torch.compile(func)
            out = compiled(inputs, pg_id=pg_id)
            correct = func(inputs, pg_id=pg_id)
            assert same(out, correct)

    def test_dynamo_trace_allreduce(self):
        def func(inp, *, pg_id):
            ar = torch.ops.aten.all_reduce(inp, group_id=pg_id, reduce_op="sum")
            return ar

        pg = _get_default_group()
        pg_id = _register_process_group(pg)
        inputs = torch.ones(4, 4, device="cuda")
        counter = CompileCounter()
        with enable_python_dispatcher():
            compiled = torch.compile(func, backend=counter)
            out = compiled(inputs, pg_id=pg_id)
            correct = func(inputs, pg_id=pg_id)
            assert counter.frame_count == 1
            assert counter.op_count == 1
            assert same(out, correct)


    def test_backwards(self):
        """
        It's probably not that common to need backwards support for collectives.

        However, I wanted to at least see if it was possible to support it as a design goal.
        """
        def func(inp, *, pg_id):
            ar = torch.ops.aten.all_reduce(inp, group_id=pg_id, reduce_op="sum")
            return ar

        pg = _get_default_group()
        pg_id = _register_process_group(pg)
        input = torch.ones(4, 4, device="cuda", requires_grad=True)
        with enable_python_dispatcher():
            compiled = torch.compile(func, backend="aot_eager")  # inductor bug with single-op allreduce graph
            out = compiled(input, pg_id=pg_id)
            out.sum().backward()

            correct_input = input.clone().detach().requires_grad_()
            correct = func(correct_input, pg_id=pg_id)
            correct.sum().backward()
            assert same(out, correct)
            assert same(input.grad, correct_input.grad)

    def test_meta(self):
        x = torch.rand((2, 3, 4), device="meta")
        pg = _get_default_group()
        pg_id = _register_process_group(pg)
        out = torch.ops.aten.all_reduce(x, group_id=pg_id, reduce_op="sum")
        assert x.size() == out.size()

if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    run_tests()
