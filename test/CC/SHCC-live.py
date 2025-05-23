#!/usr/bin/env python
#
# MIT License
#
# Copyright The SCons Foundation
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
Test the C shared-object compiler name variable $SHCC.
This is a live test, calling the detected C compiler via a wrapper.
"""

import TestSCons

_python_ = TestSCons._python_

test = TestSCons.TestSCons()

test.file_fixture('wrapper.py')
test.file_fixture('CC-fixture/foo.c')
test.file_fixture('CC-fixture/bar.c')

test.write('SConstruct', f"""\
DefaultEnvironment(tools=[])
foo = Environment()

shcc = foo['SHCC']
bar = Environment(SHCC=r'{_python_} wrapper.py ' + shcc)
foo.SharedObject(target='foo/foo', source='foo.c')
bar.SharedObject(target='bar/bar', source='bar.c')
""")

test.run(arguments='foo')
test.must_not_exist(test.workpath('wrapper.out'))

test.run(arguments='bar')
test.must_match('wrapper.out', "wrapper.py\n", mode='r')

test.pass_test()

# Local Variables:
# tab-width:4
# indent-tabs-mode:nil
# End:
# vim: set expandtab tabstop=4 shiftwidth=4:
