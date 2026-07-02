import unittest

from court_auction_crawler.parser import parse_items_from_html


class ParserTests(unittest.TestCase):
    def test_parse_items_from_html_picks_auction_table(self):
        html = """
        <html>
          <body>
            <table><tr><td>메뉴</td></tr></table>
            <table>
              <tr>
                <th>사건번호</th><th>물건번호</th><th>소재지</th><th>최저매각가격</th>
              </tr>
              <tr>
                <td>2025타경1234</td><td>1</td><td>서울시 중구</td><td>100,000,000원</td>
              </tr>
            </table>
          </body>
        </html>
        """

        items = parse_items_from_html(html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].normalized()["사건번호"], "2025타경1234")
        self.assertEqual(items[0].normalized()["소재지"], "서울시 중구")


if __name__ == "__main__":
    unittest.main()
