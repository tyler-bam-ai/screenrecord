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
        message: 'Restart command sent to ' + computerName
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
