# Team checks

<!-- Rules every page of the space must follow, checked without AI: in Health, at each
write, and by the nightly tidy-up. Each level-2 heading is a rule; the list right under it
describes it:
- pattern: a regular expression (RE2 syntax), between backticks;
- field and expect: a front matter field, expected `present`, a YAML `list` or a `date`;
- preset: email, phone, card or iban (personal data, masked in reports);
- allow: values (or email domains) never reported;
- paths and except: folders or globs, relative to content/ (`clients/`, `notes/**/*.md`);
- level: warn (reported) or block (the write is refused);
- message: what to do.
- fix: yes or no, whether the nightly maintenance fixes what people break (by default,
  the block rules and personal data).
Delete the rules you do not need, adapt the others. -->

## No nested brackets in a link label
- pattern: `\[[^\]\n]*\[[^\]\n]*\][^\]\n]*\]\(`
- message: Remove the brackets from the link text: they break the link.

## Tags are a YAML list
- field: tags
- expect: list
- message: Write `tags: [a, b]`, or one `- tag` per line.

## Sources are a YAML list
- field: sources
- expect: list
- message: Write `sources: [a, b]`, or one `- source` per line.

## Every page has a type
- field: type
- expect: present
- paths: notes/
- message: Add `type:` to the front matter (meeting, procedure, decision…).

## Update date without a time
- field: updated
- expect: date
- message: Write the date as YYYY-MM-DD, without the time.

## No local working path
- pattern: `/private/tmp|scratchpad`
- level: block
- message: Remove the local path: link to the page or the file instead.

## No personal email address
- preset: email
- allow: example.com
- level: block
- message: Write the person's name and link to their page, not their address.

## No phone number
- preset: phone
- message: Keep phone numbers in the directory, not in the wiki.

## No card number
- preset: card
- level: block
- message: Never write a card number: remove it.

## No IBAN
- preset: iban
- paths: suppliers/
- message: Bank details go to accounting, not to the wiki.
