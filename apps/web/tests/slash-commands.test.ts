import assert from "node:assert/strict";
import test from "node:test";

import { slashCommands, suggestSlashCommands } from "../lib/slash-commands.ts";

test("shows every registered command for a bare slash", () => {
  assert.deepEqual(suggestSlashCommands("/"), slashCommands);
});

test("filters commands by their Chinese name", () => {
  assert.deepEqual(
    suggestSlashCommands("/搜").map((command) => command.name),
    ["/搜索"],
  );
});

test("closes suggestions once arguments begin", () => {
  assert.deepEqual(suggestSlashCommands("/搜索 Unity"), []);
});

test("does not treat ordinary text as a command", () => {
  assert.deepEqual(suggestSlashCommands("帮我搜索 Unity"), []);
});
