from __future__ import print_function
import sys
import os
import argparse
import re
import binascii
import struct
import time
from . import database
from .database.utils import format_and
from .database.utils import start_bit
from . import tester
from .compat import fopen

# Remove once less users are using the old package structure.
from . import database as db


__author__ = 'Erik Moqvist'
__version__ = '28.9.0'


# Matches 'candump' output, i.e. "vcan0  1F0   [8]  00 00 00 00 00 00 1B C1".
RE_CANDUMP = re.compile(r'^.*  ([0-9A-F]+)   \[\d+\]\s*([0-9A-F ]*)$')

MULTI_LINE_FMT = '''
{message}(
{signals}
)\
'''

GENERATE_H_FMT = '''\
/**
 * The MIT License (MIT)
 *
 * Copyright (c) 2018 Erik Moqvist
 *
 * Permission is hereby granted, free of charge, to any person
 * obtaining a copy of this software and associated documentation
 * files (the "Software"), to deal in the Software without
 * restriction, including without limitation the rights to use, copy,
 * modify, merge, publish, distribute, sublicense, and/or sell copies
 * of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be
 * included in all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
 * BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
 * ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 * CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

/**
 * This file was generated by cantools version {version} {date}.
 */

#ifndef {include_guard}
#define {include_guard}

#include <stdint.h>
#include <unistd.h>

#ifndef EINVAL
#    define EINVAL -22
#endif

{structs}
{declarations}
#endif
'''

GENERATE_C_FMT = '''\
/**
 * The MIT License (MIT)
 *
 * Copyright (c) 2018 Erik Moqvist
 *
 * Permission is hereby granted, free of charge, to any person
 * obtaining a copy of this software and associated documentation
 * files (the "Software"), to deal in the Software without
 * restriction, including without limitation the rights to use, copy,
 * modify, merge, publish, distribute, sublicense, and/or sell copies
 * of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be
 * included in all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
 * BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
 * ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 * CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

/**
 * This file was generated by cantools version {version} {date}.
 */

#include <string.h>

#include "{header}"

{definitions}\
'''

STRUCT_FMT = '''\
/**
 * Signals in message {database_message_name}.
 *
{comments}
 */
struct {database_name}_{message_name}_t {{
{members}
}};
'''

DECLARATION_FMT = '''\
/**
 * Encode message {database_message_name}.
 *
 * @param[out] dst_p Buffer to encode the message into.
 * @param[in] src_p Data to encode.
 * @param[in] size Size of dst_p.
 *
 * @return Size of encoded data, or negative error code.
 */
ssize_t {database_name}_{message_name}_encode(
    uint8_t *dst_p,
    struct {database_name}_{message_name}_t *src_p,
    size_t size);

/**
 * Decode message {database_message_name}.
 *
 * @param[out] dst_p Object to decode the message into.
 * @param[in] src_p Message to decode.
 * @param[in] size Size of src_p.
 *
 * @return zero(0) or negative error code.
 */
int {database_name}_{message_name}_decode(
    struct {database_name}_{message_name}_t *dst_p,
    uint8_t *src_p,
    size_t size);
'''

DEFINITION_FMT = '''\
ssize_t {database_name}_{message_name}_encode(
    uint8_t *dst_p,
    struct {database_name}_{message_name}_t *src_p,
    size_t size)
{{
    if (size < {message_length}) {{
        return (-EINVAL);
    }}

    memset(&dst_p[0], 0, {message_length});

{encode_code}

    return ({message_length});
}}

int {database_name}_{message_name}_decode(
    struct {database_name}_{message_name}_t *dst_p,
    uint8_t *src_p,
    size_t size)
{{
    if (size < {message_length}) {{
        return (-EINVAL);
    }}

    memset(dst_p, 0, sizeof(*dst_p));

{decode_code}

    return (0);
}}
'''

SIGN_EXTENSION_FMT = '''
    if (dst_p->{name} & (1 << {shift})) {{
        dst_p->{name} |= {mask};
    }}

'''


def _mo_unpack(mo):
    frame_id = mo.group(1)
    frame_id = '0' * (8 - len(frame_id)) + frame_id
    frame_id = binascii.unhexlify(frame_id)
    frame_id = struct.unpack('>I', frame_id)[0]
    data = mo.group(2)
    data = data.replace(' ', '')
    data = binascii.unhexlify(data)

    return frame_id, data


def _format_signals(message, decoded_signals):
    formatted_signals = []

    for signal in message.signals:
        try:
            value = decoded_signals[signal.name]
        except KeyError:
            continue

        if isinstance(value, str):
            value = "'{}'".format(value)

        formatted_signals.append(
            '{}: {}{}'.format(signal.name,
                               value,
                              ''
                              if signal.unit is None
                              else ' ' + signal.unit))

    return formatted_signals


def _format_message_single_line(message, formatted_signals):
    return ' {}({})'.format(message.name,
                            ', '.join(formatted_signals))


def _format_message_multi_line(message, formatted_signals):
    indented_signals = [
        '    ' + formatted_signal
        for formatted_signal in formatted_signals
    ]

    return MULTI_LINE_FMT.format(message=message.name,
                                 signals=',\n'.join(indented_signals))


def _format_message(dbase, frame_id, data, decode_choices, single_line):
    try:
        message = dbase.get_message_by_frame_id(frame_id)
    except KeyError:
        return ' Unknown frame id {0} (0x{0:x})'.format(frame_id)

    try:
        decoded_signals = message.decode(data, decode_choices)
    except Exception as e:
        return ' ' + str(e)

    formatted_signals = _format_signals(message, decoded_signals)

    if single_line:
        return _format_message_single_line(message, formatted_signals)
    else:
        return _format_message_multi_line(message, formatted_signals)


def _do_decode(args):
    dbase = database.load_file(args.database,
                               encoding=args.encoding,
                               frame_id_mask=args.frame_id_mask,
                               strict=not args.no_strict)
    decode_choices = not args.no_decode_choices

    while True:
        line = sys.stdin.readline()

        # Break at EOF.
        if not line:
            break

        line = line.strip('\r\n')
        mo = RE_CANDUMP.match(line)

        if mo:
            frame_id, data = _mo_unpack(mo)
            line += ' ::'
            line += _format_message(dbase,
                                    frame_id,
                                    data,
                                    decode_choices,
                                    args.single_line)

        print(line)


def _do_dump(args):
    dbase = database.load_file(args.database,
                               encoding=args.encoding,
                               strict=not args.no_strict)

    print('================================= Messages =================================')
    print()
    print('  ' + 72 * '-')

    for message in dbase.messages:
        cycle_time = message.cycle_time

        if cycle_time is None:
            cycle_time = '-'

        if len(message.senders) == 0:
            message.senders.append('-')

        print()
        print('  Name:       {}'.format(message.name))
        print('  Id:         0x{:x}'.format(message.frame_id))
        print('  Length:     {} bytes'.format(message.length))
        print('  Cycle time: {} ms'.format(cycle_time))
        print('  Senders:    {}'.format(format_and(message.senders)))
        print('  Layout:')
        print()
        print('\n'.join([
            ('    ' + line).rstrip()
            for line in message.layout_string().splitlines()
        ]))
        print()
        print('  Signal tree:')
        print()
        print('\n'.join([
            ('    ' + line).rstrip()
            for line in message.signal_tree_string().splitlines()
        ]))
        print()
        print('  ' + 72 * '-')


def _do_convert(args):
    dbase = database.load_file(args.infile,
                               encoding=args.encoding,
                               strict=not args.no_strict)

    output_format = os.path.splitext(args.outfile)[1][1:]

    if output_format == 'dbc':
        output = dbase.as_dbc_string()
    elif output_format == 'kcd':
        output = dbase.as_kcd_string()
    else:
        sys.exit(
            "Unsupported output database format '{}'.".format(
                output_format))

    with fopen(args.outfile, 'w', encoding=args.encoding) as fout:
        fout.write(output)


def _camel_to_snake_case(value):
    value = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', value)
    value = re.sub('(_+)', r'_', value)
    value = re.sub('([a-z0-9])([A-Z])', r'\1_\2', value).lower()

    return value


def _format_comment(comment):
    if comment is None:
        return []
    else:
        return [
            ' *            ' + line.rstrip()
            for line in comment.splitlines()
        ]


def _format_minimum(minimum):
    if minimum is None:
        return []
    else:
        return [' *            Minimum: {}'.format(minimum)]


def _format_maximum(maximum):
    if maximum is None:
        return []
    else:
        return [' *            Maximum: {}'.format(maximum)]


def _format_scale(scale):
    if scale is None:
        return []
    else:
        return [' *            Scale: {}'.format(scale)]


def _format_offset(offset):
    if offset is None:
        return []
    else:
        return [' *            Offset: {}'.format(offset)]


def _format_unit(unit):
    if unit is None:
        return []
    else:
        return [' *            Unit: {}'.format(unit)]


def _generate_signal(signal):
    if signal.is_multiplexer or signal.multiplexer_ids:
        print('warning: Multiplexed signals are not yet supported.')

        return None, None

    if signal.is_float:
        print('warning: Float signals are not yet supported.')

        return None, None

    if signal.length <= 8:
        type_name = 'int8_t'
    elif signal.length <= 16:
        type_name = 'int16_t'
    elif signal.length <= 32:
        type_name = 'int32_t'
    elif signal.length <= 64:
        type_name = 'int64_t'
    else:
        print('warning: Signal lengths over 64 bits are not yet supported.')

        return None, None

    if not signal.is_signed:
        type_name = 'u' + type_name

    name = _camel_to_snake_case(signal.name)
    lines = [' * @param {}'.format(name)]
    lines += _format_comment(signal.comment)
    lines += _format_minimum(signal.minimum)
    lines += _format_maximum(signal.maximum)
    lines += _format_scale(signal.scale)
    lines += _format_offset(signal.offset)
    lines += _format_unit(signal.unit)
    comment = '\n'.join(lines)
    member = '    {} {};'.format(type_name, name)

    return comment, member


def _signal_segments(signal, invert_shift):
    index, pos = divmod(signal.start, 8)
    left = signal.length

    while left > 0:
        if signal.byte_order == 'big_endian':
            if left > (pos + 1):
                length = (pos + 1)
                pos = 7
                shift = -(left - length)
                mask = ((1 << length) - 1)
            else:
                length = left
                mask = ((1 << length) - 1)

                if (pos - length) >= 0:
                    shift = (pos - length + 1)
                else:
                    shift = (8 - left)

                mask <<= (pos - length + 1)
        else:
            if left >= (8 - pos):
                length = (8 - pos)
                shift = (left - signal.length) + pos
                mask = ((1 << length) - 1)
                mask <<= pos
                pos = 0
            else:
                length = left
                mask = ((1 << length) - 1)
                shift = pos
                mask <<= pos

        if invert_shift:
            if shift < 0:
                shift = '<< {}'.format(-shift)
            else:
                shift = '>> {}'.format(shift)
        else:
            if shift < 0:
                shift = '>> {}'.format(-shift)
            else:
                shift = '<< {}'.format(shift)

        yield index, shift, mask

        left -= length
        index += 1


def _format_encode_code(message):
    code_per_index = {}

    for signal in message.signals:
        for index, shift, mask in _signal_segments(signal, False):
            if index not in code_per_index:
                code_per_index[index] = []

            line = '    dst_p[{}] |= ((src_p->{} {}) & 0x{:02x});'.format(
                index,
                _camel_to_snake_case(signal.name),
                shift,
                mask)
            code_per_index[index].append(line)

    code = []

    for index in sorted(code_per_index):
        code += code_per_index[index]

    return '\n'.join(code)


def _format_decode_code(message):
    code = []

    for signal in message.signals:
        name = _camel_to_snake_case(signal.name)
        if signal.length <= 8:
            type_length = 8
        elif signal.length <= 16:
            type_length = 16
        elif signal.length <= 32:
            type_length = 32
        elif signal.length <= 64:
            type_length = 64

        for index, shift, mask in _signal_segments(signal, True):
            line = '    dst_p->{} |= ((uint{}_t)(src_p[{}] & 0x{:02x}) {});'.format(
                name,
                type_length,
                index,
                mask,
                shift)
            code.append(line)

        if signal.is_signed:
            mask = ((1 << (type_length - signal.length)) - 1)
            mask <<= signal.length
            formatted = SIGN_EXTENSION_FMT.format(name=name,
                                                  shift=signal.length - 1,
                                                  mask=hex(mask))
            code.extend(formatted.splitlines())

    if code[-1] == '':
        code = code[:-1]

    return '\n'.join(code)


def _generate_message(database_name, message):
    comments = []
    members = []

    for signal in message.signals:
        comment, member = _generate_signal(signal)

        if comment is not None:
            comments.append(comment)

        if member is not None:
            members.append(member)

    name = _camel_to_snake_case(message.name)
    struct_ = STRUCT_FMT.format(database_message_name=message.name,
                                message_name=name,
                                database_name=database_name,
                                comments='\n'.join(comments),
                                members='\n'.join(members))
    declaration = DECLARATION_FMT.format(database_name=database_name,
                                         database_message_name=message.name,
                                         message_name=name)
    encode_code = _format_encode_code(message)
    decode_code = _format_decode_code(message)
    definition = DEFINITION_FMT.format(database_name=database_name,
                                       database_message_name=message.name,
                                       message_name=name,
                                       message_length=message.length,
                                       encode_code=encode_code,
                                       decode_code=decode_code)

    return struct_, declaration, definition


def _do_generate_c_source(args):
    dbase = database.load_file(args.infile,
                               encoding=args.encoding,
                               strict=not args.no_strict)

    basename = os.path.basename(args.infile)
    filename = os.path.splitext(basename)[0]
    filename_h = filename + '.h'
    filename_c = filename + '.c'
    date = time.ctime()
    include_guard = '__{}_H__'.format(filename.upper())
    structs = []
    declarations = []
    definitions = []

    for message in dbase.messages:
        struct_, declaration, definition = _generate_message(filename,
                                                             message)
        structs.append(struct_)
        declarations.append(declaration)
        definitions.append(definition)

    with open(filename_h, 'w') as fout:
        fout.write(GENERATE_H_FMT.format(version=__version__,
                                         date=date,
                                         include_guard=include_guard,
                                         structs='\n'.join(structs),
                                         declarations='\n'.join(declarations)))

    with open(filename_c, 'w') as fout:
        fout.write(GENERATE_C_FMT.format(version=__version__,
                                         date=date,
                                         header=filename_h,
                                         definitions='\n'.join(definitions)))

    print('Successfully generated {} and {}.'.format(filename_h, filename_c))


def _main():
    parser = argparse.ArgumentParser(
        description='Various CAN utilities.')

    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('--version',
                        action='version',
                        version=__version__,
                        help='Print version information and exit.')

    # Workaround to make the subparser required in Python 3.
    subparsers = parser.add_subparsers(title='subcommands',
                                       dest='subcommand')
    subparsers.required = True

    # The 'decode' subparser.
    decode_parser = subparsers.add_parser(
        'decode',
        description=('Decode "candump" CAN frames read from standard input '
                     'and print them in a human readable format.'))
    decode_parser.add_argument(
        '-c', '--no-decode-choices',
        action='store_true',
        help='Do not convert scaled values to choice strings.')
    decode_parser.add_argument(
        '-s', '--single-line',
        action='store_true',
        help='Print the decoded message on a single line.')
    decode_parser.add_argument(
        '-e', '--encoding',
        default='utf-8',
        help='File encoding (default: utf-8).')
    decode_parser.add_argument(
        '--no-strict',
        action='store_true',
        help='Skip database consistency checks.')
    decode_parser.add_argument(
        '-m', '--frame-id-mask',
        type=lambda x: int(x, 0),
        help=('Only compare selected frame id bits to find the message in the '
              'database. By default the candump and database frame ids must '
              'be equal for a match.'))
    decode_parser.add_argument(
        'database',
        help='Database file.')
    decode_parser.set_defaults(func=_do_decode)

    # The 'dump' subparser.
    dump_parser = subparsers.add_parser(
        'dump',
        description='Dump given database in a human readable format.')
    dump_parser.add_argument(
        '-e', '--encoding',
        default='utf-8',
        help='File encoding (default: utf-8).')
    dump_parser.add_argument(
        '--no-strict',
        action='store_true',
        help='Skip database consistency checks.')
    dump_parser.add_argument(
        'database',
        help='Database file.')
    dump_parser.set_defaults(func=_do_dump)

    # The 'convert' subparser.
    convert_parser = subparsers.add_parser(
        'convert',
        description='Convert given database from one format to another.')
    convert_parser.add_argument(
        '-e', '--encoding',
        default='utf-8',
        help='File encoding (default: utf-8).')
    convert_parser.add_argument(
        '--no-strict',
        action='store_true',
        help='Skip database consistency checks.')
    convert_parser.add_argument(
        'infile',
        help='Input database file.')
    convert_parser.add_argument(
        'outfile',
        help='Output database file.')
    convert_parser.set_defaults(func=_do_convert)

    # The 'generate_c_source' subparser.
    generate_c_source_parser = subparsers.add_parser(
        'generate_c_source',
        description='Generate C source code from given database file.')
    generate_c_source_parser.add_argument(
        '-e', '--encoding',
        default='utf-8',
        help='File encoding (default: utf-8).')
    generate_c_source_parser.add_argument(
        '--no-strict',
        action='store_true',
        help='Skip database consistency checks.')
    generate_c_source_parser.add_argument(
        'infile',
        help='Input database file.')
    generate_c_source_parser.set_defaults(func=_do_generate_c_source)

    args = parser.parse_args()

    if args.debug:
        args.func(args)
    else:
        try:
            args.func(args)
        except BaseException as e:
            sys.exit('error: ' + str(e))
