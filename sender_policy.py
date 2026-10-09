"""按來源群及完整顯示名稱略過指定發送者，不比對正文關鍵字。"""


def ignored_senders_for_group(config, group, member_names):
    mapping = config.get('ignored_senders_by_group', {})
    if not isinstance(mapping, dict):
        raise ValueError('ignored_senders_by_group 必須是群組名稱對應姓名清單。')
    for source, names in mapping.items():
        if (not isinstance(source, str) or not source.strip()
                or not isinstance(names, list)
                or any(not isinstance(name, str) or not name.strip()
                       or '\n' in name or '\r' in name for name in names)
                or len(set(names)) != len(names)):
            raise ValueError('略過設定的群組與完整發送者姓名不可空白、重複或包含換行。')
    names = mapping.get(group, [])
    if any(name not in member_names for name in names):
        raise ValueError('要略過的發送者尚未列入已核對的成員姓名；禁止猜測卡片來源。')
    return list(names)
