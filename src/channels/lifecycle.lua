-- Single Redis-side owner of channel streams and their accounting (standalone Redis).
local heads, usage, ready, clock = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local operation, key, cutoff, cap = ARGV[1], ARGV[2], ARGV[3], tonumber(ARGV[4])
local reply, conversation = ARGV[5], ARGV[6]
if (operation ~= 'append' and operation ~= 'cleanup') or not cap or cap <= 0 or cap % 1 ~= 0
        or not string.match(cutoff, '^%d+$') or #cutoff > 20
        or (operation == 'append' and (key == '' or #ARGV < 8 or #ARGV % 2 ~= 0)) then
    return redis.error_reply('Invalid message lifecycle arguments')
end
local function kind(k) return redis.call('TYPE', k).ok end
local function check(k, expected)
    local actual = kind(k)
    if actual ~= 'none' and actual ~= expected then error('Unexpected storage key type') end
end
check(heads, 'zset'); check(usage, 'string'); check(ready, 'string'); check(clock, 'stream')
if key ~= '' then check(key, 'stream') end
local version = redis.call('GET', ready)
if version and version ~= '2' then return redis.error_reply('Unsupported message storage version') end
local function padded(n) return string.rep('0', 20 - #n) .. n end
local function parts(id)
    local ms, seq = string.match(id, '^(%d+)%-(%d+)$')
    return padded(ms) .. padded(seq)
end
local boundary = padded(cutoff) .. string.rep('0', 20)
local function member(k, id) return parts(id) .. k end
local function bytes(fields)
    local n = 0
    for _, value in ipairs(fields) do n = n + #value end
    return n
end
local fields = {}
for i = 7, #ARGV do fields[#fields + 1] = ARGV[i] end
local size = bytes(fields)
-- Reject invalid admissions before any migration, retention or capacity deletion.
if operation == 'append' then
    if size > cap then return {'oversized'} end
    if reply ~= '' then
        local row = redis.call('XRANGE', key, reply, reply, 'COUNT', 1)
        local valid = false
        if #row > 0 and parts(reply) >= boundary then
            for i = 1, #row[1][2], 2 do
                if row[1][2][i] == 'conversation_id' and row[1][2][i+1] == conversation then valid = true end
            end
        end
        if not valid then return {'reply'} end
    end
end
local total = tonumber(redis.call('GET', usage) or '0')
if not total or total < 0 then return redis.error_reply('Invalid message byte counter') end
local function add_head(k)
    local row = redis.call('XRANGE', k, '-', '+', 'COUNT', 1)
    if #row == 0 then redis.call('DEL', k)
    else redis.call('ZADD', heads, 0, member(k, row[1][1])) end
end
local function last_id(k)
    if kind(k) == 'none' then return '0-0' end
    local info = redis.call('XINFO', 'STREAM', k)
    for i = 1, #info, 2 do
        if info[i] == 'last-generated-id' then return info[i+1] end
    end
end
if kind(clock) == 'stream' and redis.call('XLEN', clock) ~= 0 then
    return redis.error_reply('Message clock must be empty')
end
local high = last_id(clock)
local keys
if version ~= '2' then
    -- Capture even empty legacy streams before any reclamation. Decimal padding
    -- compares uint64 ID components exactly, without Lua floating-point conversion.
    keys = redis.call('KEYS', 'channel:*')
    for _, k in ipairs(keys) do
        check(k, 'stream')
        local candidate = last_id(k)
        if parts(candidate) > parts(high) then high = candidate end
    end
elseif kind(clock) == 'none' then
    return redis.error_reply('Missing message clock')
end
if operation == 'append' and high == '18446744073709551615-18446744073709551615' then
    return redis.error_reply('Stream ID exhausted')
end
if keys then
    if kind(clock) == 'none' then
        redis.call('XADD', clock, '0-1', 'tick', '')
        redis.call('XDEL', clock, '0-1')
        -- The bootstrap deletion retains 0-1 as the clock's minimum native ID.
        if parts(high) < parts('0-1') then high = '0-1' end
    end
    redis.call('XSETID', clock, high)
    total = 0
    redis.call('DEL', heads, usage)
    for _, k in ipairs(keys) do
        for _, row in ipairs(redis.call('XRANGE', k, '-', '+')) do total = total + bytes(row[2]) end
        add_head(k)
    end
end
local id
if operation == 'append' then
    local empty = redis.call('XLEN', key) == 0
    id = redis.call('XADD', clock, '*', 'tick', '')
    redis.call('XDEL', clock, id)
    redis.call('XADD', key, id, unpack(fields))
    total = total + size
    if empty then add_head(key) end
end
while true do
    local first = redis.call('ZRANGE', heads, 0, 0)[1]
    if not first or (total <= cap and string.sub(first, 1, 40) >= boundary) then break end
    local oldest_key = string.sub(first, 41)
    local row = redis.call('XRANGE', oldest_key, '-', '+', 'COUNT', 1)[1]
    redis.call('ZREM', heads, first)
    if row then
        total = total - bytes(row[2])
        redis.call('XDEL', oldest_key, row[1])
    end
    add_head(oldest_key)
end
if total == 0 then redis.call('DEL', usage) else redis.call('SET', usage, string.format('%.0f', total)) end
-- Commit migration version only after the lifecycle has completed.
redis.call('SET', ready, '2')
return {'ok', id or ''}
