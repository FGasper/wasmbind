from typing import List

import pytest

from wasmbind.module import AssemblyScriptArray, OpaqueValue


def test_strings(from_code):
    module = from_code("""
    export function helloworld(s: string): string {
        return "foo:" + s
    }
    """)
    assert module.helloworld("foo", as_=str) == 'foo:foo'


def test_empty_string(from_code):
    module = from_code("""
    export function helloworld(s: string): string {
        return s
    }
    """)

    assert module.helloworld("", as_=str) == ''


def test_root_function(from_code):
    module = from_code("""
    export function sum(a: i32, b: i32): i32 {
        return a + b;
    }
    """)
    assert module.sum(1, 2) == 3


def test_properties(from_code):
    module = from_code("""
    export class Foo {
        constructor() {}
        public bar: i32 = 42;        
    }
    """)
    foo = module.Foo()
    assert foo.bar == 42
    foo.bar = 13
    assert foo.bar == 13


def test_return_an_object(from_code):
    module = from_code("""
    export class Foo {
        constructor() {}
        public bar: i32 = 42;        
    }
    export function getFoo(): Foo {
        return new Foo();        
    }
    """)

    assert isinstance(module.getFoo(), int)

    foo = module.getFoo(as_=module.Foo)
    assert foo.bar == 42


class TestResolve:

    def test_auto_resolve_string(self, from_code):
        module = from_code("""            
        export function getS(): string { return "foo"; }
        """)
        assert module.resolve(module.getS()) == "foo";

    def test_resolve_integer_list(self, from_code):
        module = from_code("""            
        export function getList(): u8[] {
            return [1,2,6];
        }
        """)
        assert module.resolve(module.getList()) == [1,2,6]

    def test_resolve_object_list(self, from_code):
        module = from_code("""
        export class Foo { constructor(public x: u32) {} }            
        export function getList(): Foo[] {
            return [new Foo(1), new Foo(4), new Foo(8)];
        }
        """)
        array = module.resolve(module.getList(), as_=List[module.Foo])
        assert [a.x for a in array] == [1, 4, 8]


class TestRTTI:

    def test_get_type_from_pointer(self, from_code):
        module = from_code("""
        export class Foo { constructor() {} }
        export class Bar extends Foo { constructor() { super(); } }        
        """)
        foo_type = module.get_type_of(module.Foo())
        bar_type = module.get_type_of(module.Bar())
        assert bar_type.base_id == foo_type.id


class TestArrays:
    def test_return_array(self, from_code):
        module = from_code("""
        export class Foo { 
            constructor() {}
            getNumbers(): u32[] {
                return [9, 3, 1];
            }
        }        
        """)
        assert isinstance(module.Foo().getNumbers(), int)

    def test_array_of_strings(self, from_code):
        module = from_code("""
        export function rungc(): void { gc.collect(); }
        export const StringArray = idof<string[]>();        
        """)

        # If the array does not hold a reference to it's strings, 
        # we'd have a crash during __del__
        arr = module.alloc_array(module.StringArray, ["sdf", "foo"])

        # There are two references. One is the array, one is the string
        # returned by arr[0]
        assert module.get_refcount_of(arr[0]) == 2
    
        assert arr.as_(List[str])[0] == "sdf"
        assert arr[0].as_(str) == "sdf"
        assert arr[1].as_(str) == "foo"

    def test_alloc_invalid_array(self, from_code):
        module = from_code("""
        export class Foo { constructor() {} }       
        export const Int8ArrayId = idof<Foo>();        
        """)
        with pytest.raises(TypeError):
            module.alloc_array(module.Int8ArrayId, [1, 2, 3])

    def test_alloc_and_pass_array(self, from_code):
        module = from_code("""
        export const Int8ArrayId = idof<Array<u8>>();
        export function sum(arg: u8[]): u8 {
            return arg.reduce((a, b) => a + b, 0) as u8;
        };
        """)

        # Create an array in WASM memory on the Python side
        array = module.alloc_array(module.Int8ArrayId, [1, 2, 3])
        assert module.get_refcount_of(array) == 1

        # Index access
        assert array[0] == 1
        assert array[1:3] == [2, 3]

        # Can pass array to WASM
        assert module.sum(array) == 6

        # Can change the array in Python
        array[1:3] = [8, 5]
        assert module.sum(array) == 14

    def test_access_wasm_created_array(self, from_code):
        module = from_code("""        
        export function getFoo(): i32[] {
            return [1,4]
        }
        """)

        # TODO: Also test gc, to make sure we keep a reference while we have it.

    def test_alloc_and_pass_array_of_references(self, from_code):
        module = from_code("""        
        export class Foo { constructor(public x: i32) {} }       
        export const FooArrayId = idof<Foo[]>();
        
        export function getItem(data: Foo[], idx: i32): i32 {
            return data[idx].x;
        }
        """)

        foo_array = module.alloc_array(
            module.FooArrayId, [module.Foo(3), module.Foo(4)])

        assert foo_array[0].as_(module.Foo).x == 3

        assert len(foo_array) == 2
        assert module.getItem(foo_array, 0) == 3
        assert module.getItem(foo_array, 1) == 4


class TestGarbageCollect:

    def test_manual_retain_calls(self, from_code):
        module = from_code("""
        export class Foo { constructor() {} }        
        """)

        foo = module.Foo()
        foo_pointer = module.get_pointer(foo)

        assert module.get_refcount_of(foo_pointer) == 1
        module.retain(foo_pointer)
        assert module.get_refcount_of(foo_pointer) == 2
        module.release(foo_pointer)
        assert module.get_refcount_of(foo_pointer) == 1

    def test_release_on_del(self, from_code):
        module = from_code("""
        export class Foo { constructor(public x: i32) {} }        
        export function collect(): void { gc.collect() }
        """)

        foo = module.Foo(5)
        foo_pointer = module.get_pointer(foo)
        assert module.get_refcount_of(foo_pointer) == 1

        # Trigger a delete
        del foo

        # Note: This seems to be the expected behaviour. The memory gets marked as cleared, but
        # the compiler does not waste effort to set the ref count to 0.
        assert module.get_refcount_of(foo_pointer) == 1

        # However, by creating a new object we can verify that it will be put in the same place
        # as the now deleted on.
        bar = module.Foo(9)
        bar_pointer = module.get_pointer(bar)
        assert bar_pointer == foo_pointer


def test_opaque_values(from_code):
    module = from_code("""
    export function take(val: i32): i32 { return val; }
    """)

    # weakref disabled
    my_map = {"x": 1}
    wrapped_map = module.register_opaque_value(my_map)
    assert module.take(wrapped_map, as_=OpaqueValue) == {"x": 1}

    # weakref enabled
    class Foo:
        x = 1
    v = Foo()
    wrapped_map = module.register_opaque_value(v)
    assert module.take(wrapped_map, as_=OpaqueValue) == v

    wrapped_map = module.register_opaque_value(Foo())
    with pytest.raises(ValueError):
        assert module.take(wrapped_map, as_=OpaqueValue).x == 1


def test_pass_objects_as_arguments(from_code):
    module = from_code("""
    export class Line {
      constructor(
        public s: string
      ) {}
    }

    export class File {
      public lines: Line[] = []

      constructor() {}

      addLine(line: Line): number {
        this.lines.push(line);
        return this.lines.length; 
      }
    }
    """)

    file = module.File()

    assert file.addLine(module.Line("line 1")) == 1
    assert file.addLine(module.Line("line 2")) == 2