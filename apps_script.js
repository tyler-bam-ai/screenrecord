/**
 * Google Apps Script - Restart Command Handler
 *
 * Deploy this as a web app attached to your Screen Recording Dashboard sheet.
 *
 * Setup:
 * 1. Open your Google Sheet → Extensions → Apps Script
 * 2. Paste this code
 * 3. Click Deploy → New deployment → Web app
 * 4. Set "Execute as" = Me, "Who has access" = Anyone
 * 5. Click Deploy and copy the URL
 * 6. Paste the URL into your dashboard's APPS_SCRIPT_URL config
 */

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var computerName = data.computer_name;
    var command = data.command || 'restart';

    if (!computerName) {
      return ContentService.createTextOutput(
        JSON.stringify({ success: false, error: 'Missing computer_name' })
      ).setMimeType(ContentService.MimeType.JSON);
    }

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    // ---- Editable machine fields (clinic name, employee name) ----------------
    // Dashboard sends { action:'set_field', computer_name, field, value }. The
    // agent preserves client_name/employee_name, so a manual edit here sticks.
    if (data.action === 'set_field') {
      var FIELD_COL = { client_name: 3, employee_name: 2 };  // Machines cols A..H
      var col = FIELD_COL[data.field];
      if (!col) {
        return ContentService.createTextOutput(
          JSON.stringify({ success: false, error: 'Unknown field: ' + data.field })
        ).setMimeType(ContentService.MimeType.JSON);
      }
      var msheet = ss.getSheetByName('Machines');
      if (!msheet) {
        return ContentService.createTextOutput(
          JSON.stringify({ success: false, error: 'Machines sheet not found' })
        ).setMimeType(ContentService.MimeType.JSON);
      }
      var names = msheet.getRange('A:A').getValues();
      for (var i = 1; i < names.length; i++) {
        if (names[i][0] === computerName) {
          msheet.getRange(i + 1, col).setValue(data.value || '');
          return ContentService.createTextOutput(
            JSON.stringify({ success: true, message: 'Updated ' + data.field })
          ).setMimeType(ContentService.MimeType.JSON);
        }
      }
      return ContentService.createTextOutput(
        JSON.stringify({ success: false, error: 'Machine not found: ' + computerName })
      ).setMimeType(ContentService.MimeType.JSON);
    }

    // ---- Default: queue a remote command (restart/stop/start/record_test) ----
    var sheet = ss.getSheetByName('Commands');

    if (!sheet) {
      return ContentService.createTextOutput(
        JSON.stringify({ success: false, error: 'Commands sheet not found' })
      ).setMimeType(ContentService.MimeType.JSON);
    }

    // Append the command
    var timestamp = new Date().toISOString();
    sheet.appendRow([timestamp, computerName, command, 'pending', '']);

    return ContentService.createTextOutput(
      JSON.stringify({
        success: true,
        message: 'Command sent to ' + computerName
      })
    ).setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService.createTextOutput(
      JSON.stringify({ success: false, error: err.toString() })
    ).setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  return ContentService.createTextOutput(
    JSON.stringify({ status: 'ok', message: 'Screen Recording Command API' })
  ).setMimeType(ContentService.MimeType.JSON);
}
